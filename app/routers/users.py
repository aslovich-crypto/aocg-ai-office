from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth import (
    Role,
    create_access_token,
    create_refresh_token,
    get_current_user,
    hash_password,
    verify_password,
)
from app.routers.auth import _require_admin
from app.database import get_pool
from app.sql_builder import собрать_set

router = APIRouter(prefix="/api/users", tags=["users"])

# Что человек правит В СВОЁМ профиле (PATCH /me). Отдельно от UPDATABLE:
# у себя можно менять телефон и табельный, но не ИНН и не роль.
СВОЙ_ПРОФИЛЬ = ("first_name", "last_name", "phone", "employee_number")

# Columns the admin PATCH /{id} (employee management) may touch.
UPDATABLE = (
    "first_name",
    "last_name",
    "patronymic",
    "email",
    "inn",
    "region",
    "employee_id",
    # ⚠️ T118, 31.08.2026. РОЛЬ ЗДЕСЬ ОТСУТСТВОВАЛА, и это делало повышение
    # человека невозможным: единственным способом было удалить строку и
    # завести заново — то есть потерять его чеки и отчёты. Довод владельца:
    # приглашают человека ОДИН раз, а роли меняют постоянно, по мере того
    # как люди берут на себя больше.
    "role",
)

# Never expose secrets in user payloads.
_HIDDEN = ("password_hash", "email_verify_token")

# S-28: что видит РЯДОВОЙ сотрудник в списке коллег. Полная строка — это
# кадровая карточка (email, ИНН, регион, табельный номер, роль, флаги);
# интерфейсу от неё нужны только подпись автора отчёта и выбор в фильтрах,
# то есть id и ФИО. Список белый, а не чёрный: новая колонка в users
# не утечёт сама собой.
_EMPLOYEE_VISIBLE = ("id", "first_name", "last_name", "patronymic")

# Кому список отдаётся целиком. Роль вне этого набора (в т.ч. неизвестная
# и будущая manager из Финансов) получает урезанную форму — безопасный
# умолчательный ответ.
_FULL_VIEW_ROLES = ("admin", "accountant")


# ⚠️ БЕЛЫЙ СПИСОК, А НЕ ЧЁРНЫЙ (S-72, 04.09.2026). Раньше здесь стоял
# чёрный: «отдать всё, кроме пароля и токена подтверждения». Это не одна
# утечка, а КЛАСС: любая новая колонка в `users` уезжает наружу сама собой,
# и заметить это можно только вручную. Так наружу уходили `failed_attempts`
# и `locked_until` — состояние защиты, видимое тому, от кого защищаются.
#
# Что осталось за списком намеренно: счётчики неудачных входов и блокировка
# (состояние защиты), `email_verify_expires_at` и `tokens_valid_from`
# (внутренние отметки), `password_hash` и `email_verify_token` (секреты).
# Управляющему нужна кадровая карточка, а не внутренняя механика.
_MANAGER_VISIBLE = (
    "id",
    "first_name",
    "last_name",
    "patronymic",
    "email",
    "phone",
    "inn",
    "region",
    "employee_id",
    "employee_number",
    "role",
    "is_active",
    "is_email_verified",
    "org_id",
    "created_at",
    "last_login_at",
)


def _safe(row) -> dict:
    d = dict(row)
    return {k: d.get(k) for k in _MANAGER_VISIBLE}


def _by_role(row, viewer_role: Optional[str]) -> dict:
    """Форма записи о пользователе с оглядкой на роль СМОТРЯЩЕГО (S-28)."""
    if viewer_role in _FULL_VIEW_ROLES:
        return _safe(row)
    d = dict(row)
    return {k: d.get(k) for k in _EMPLOYEE_VISIBLE}


async def _me_payload(p, u: dict) -> dict:
    """Profile shape for GET/PATCH /me, including latest consent."""
    # TODO(auth-migration): user_consents с user_id='local_user' — legacy
    # из периода до авторизации. После полного перевода всех users
    # на JWT выполнить миграцию:
    #   SELECT consent → match by email → UPDATE user_id → DELETE legacy.
    # 152-ФЗ требует чёткой привязки согласия к user_id.
    # S-34: тянем и САМ ТЕКСТ. Настройки показывали текущую редакцию под
    # старую запись — то есть снова «не то, на что соглашались». Запись
    # обязана воспроизводиться дословно, для этого текст и заморожен.
    consent_row = await p.fetchrow(
        """SELECT consent_at, policy_version, consent_text FROM user_consents
           WHERE user_id IN ($1, $2, 'local_user')
           ORDER BY consent_at DESC LIMIT 1""",
        str(u["id"]),
        (u.get("email") or ""),
    )
    consent = None
    if consent_row:
        consent = {
            "given_at": consent_row["consent_at"].isoformat()
            if consent_row["consent_at"]
            else None,
            "policy_version": consent_row["policy_version"],
            "text": consent_row["consent_text"],
        }
    return {
        "id": u["id"],
        "first_name": u.get("first_name"),
        "last_name": u.get("last_name"),
        "email": u.get("email"),
        "phone": u.get("phone"),
        "employee_number": u.get("employee_number"),
        "role": u.get("role"),
        "is_email_verified": u.get("is_email_verified"),
        "linked_providers": [],  # OAuth not wired yet
        "consent": consent,
    }


class UserUpdate(BaseModel):
    # ⚠️ `Role`, А НЕ `str` — тот же белый список, что у приглашения (S-24).
    # Через PATCH роль приезжает в ту же колонку `users.role`, и произвольная
    # строка здесь дала бы ровно то, от чего S-24 закрывала другую дверь:
    # роль есть, а прав нет, потому что гейты сравнивают по равенству.
    role: Optional[Role] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    patronymic: Optional[str] = None
    email: Optional[str] = None
    inn: Optional[str] = None
    region: Optional[str] = None
    employee_id: Optional[str] = None


class MeUpdate(BaseModel):
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    phone: Optional[str] = None
    employee_number: Optional[str] = None


class PasswordChange(BaseModel):
    old_password: str
    new_password: str


@router.get("/")
async def get_users(user: dict = Depends(get_current_user)):
    """Active users of the caller's organization, oldest first.

    S-28: гейта роли здесь нет намеренно — список нужен КАЖДОМУ (подпись
    автора на своих же отчётах), поэтому режется не доступ, а форма ответа.
    """
    p = await get_pool()
    # ⚠️ ОТКЛЮЧЁННЫЕ ВИДНЫ ТЕМ, КТО УПРАВЛЯЕТ. До 31.08.2026 список читал
    # `WHERE is_active = true` всегда, и погашенный человек ИСЧЕЗАЛ С ЭКРАНА
    # целиком: свайп срабатывает легко, отменить нельзя, а увидеть, кого
    # погасил, — тоже нельзя. Это не «нет кнопки возврата», это потеря
    # наблюдаемости: ошибку не видно даже в ту же секунду.
    #
    # ⚠️ ТОЛЬКО ДЛЯ admin/accountant, И ЭТО НАМЕРЕННО. У остальных ролей
    # управляющего экрана нет, зато `users` кормит подстановку имён на
    # других экранах — расширять выдачу всем значило бы менять поведение
    # там, где меня не просили. Сотрудник получает прежний ответ байт в байт.
    if user.get("role") in _FULL_VIEW_ROLES:
        rows = await p.fetch(
            "SELECT * FROM users WHERE org_id=$1 ORDER BY is_active DESC, id",
            user["org_id"],
        )
    else:
        rows = await p.fetch(
            "SELECT * FROM users WHERE is_active = true AND org_id=$1 ORDER BY id",
            user["org_id"],
        )
    return [_by_role(r, user.get("role")) for r in rows]


# ─── /me (must be declared before /{id}) ───
@router.get("/me")
async def get_me(user: dict = Depends(get_current_user)):
    """Current user's profile."""
    p = await get_pool()
    return await _me_payload(p, user)


@router.patch("/me")
async def update_me(u: MeUpdate, user: dict = Depends(get_current_user)):
    """Self-service profile edit. Only first_name/last_name/phone/employee_number."""
    fields = {
        k: v for k, v in u.model_dump(exclude_unset=True).items() if k in СВОЙ_ПРОФИЛЬ
    }
    p = await get_pool()
    if fields:
        sets, values = собрать_set(fields, СВОЙ_ПРОФИЛЬ)
        await p.execute(
            f"UPDATE users SET {sets} WHERE id = ${len(values) + 1}",
            *values,
            user["id"],
        )
    fresh = await p.fetchrow("SELECT * FROM users WHERE id=$1", user["id"])
    return await _me_payload(p, dict(fresh))


@router.post("/me/change-password")
async def change_password(body: PasswordChange, user: dict = Depends(get_current_user)):
    if not verify_password(body.old_password, user.get("password_hash")):
        raise HTTPException(status_code=400, detail="Текущий пароль неверный")
    if not body.new_password or len(body.new_password) < 8:
        raise HTTPException(
            status_code=400, detail="Новый пароль должен быть не менее 8 символов"
        )
    p = await get_pool()
    # СМЕНА ПАРОЛЯ ГАСИТ ВСЕ ПРЕЖНИЕ ТОКЕНЫ (S-16). До 10.08.2026 здесь
    # обновлялся только хеш, и это была дыра ровно в том случае, ради
    # которого пароль и меняют: «зашли из чужого места» — а у зашедшего
    # access жил ещё час, refresh — тридцать дней, и в чёрный список
    # не попадал никогда.
    #
    # Момент берём из ПРИЛОЖЕНИЯ, а не `NOW()` базы, и ставим ДО выдачи
    # новых токенов: подпись делают часы приложения, и при расхождении
    # часов свежий токен оказался бы «старше» отметки и отвергался сразу.
    момент = datetime.now(timezone.utc)
    await p.execute(
        "UPDATE users SET password_hash=$1, tokens_valid_from=$2 WHERE id=$3",
        hash_password(body.new_password),
        момент,
        user["id"],
    )
    # ⚠️ И ГАСИМ НЕВОСТРЕБОВАННЫЕ ССЫЛКИ ВОССТАНОВЛЕНИЯ (S-56). Человек мог
    # запросить письмо час назад, не дождаться и сменить пароль сам — ссылка
    # из того письма осталась бы рабочей, то есть ключом от учётной записи
    # с УЖЕ СМЕНЁННЫМ паролем. Связь между двумя местами неочевидна и первой
    # же забудется, поэтому она закреплена тестом.
    await p.execute(
        "UPDATE password_resets SET used_at=$1 WHERE user_id=$2 AND used_at IS NULL",
        момент,
        user["id"],
    )
    # Вызывающему сразу отдаём НОВУЮ пару: иначе человек, сменивший пароль,
    # вылетал бы из системы в тот же миг. Фронт, который их не сохранит,
    # просто попросит войти заново — направление безопасное.
    return {
        "ok": True,
        "access_token": create_access_token(user["id"]),
        "refresh_token": create_refresh_token(user["id"]),
    }


# ⚠️ `POST /api/users/` СНЕСЕНА 31.08.2026 (T104, этап ④). Она заводила
# человека БЕЗ ПАРОЛЯ и без письма: строка в `users` появлялась, а войти по ней
# было нельзя — приглашение не выписывалось, пароль не задавался. Именно так
# 30.08 «завёлся» Анис Ламри, которого потом не нашли ни в списке, ни в базе
# по его почте (замер владельца: `al@aocg.ru` — 0 строк).
#
# ⚠️ ЕДИНСТВЕННЫЙ ПУТЬ ЗАВЕСТИ ЧЕЛОВЕКА ТЕПЕРЬ ОДИН — `POST /api/invite/create`.
# Два пути к одному итогу, из которых первый молча ломал вход, — это не выбор,
# а мина. Гейты, что стояли здесь (только админ; роль из белого списка `Role`),
# на приглашении стоят те же, и тесты прав переведены на него, а не удалены.
#
# ⚠️ ЗАМЕР ПЕРЕД СНОСОМ: вызовов из фронта — НОЛЬ (форма сотрудника снесена
# этапом ③), из тестов — четыре, все про права, все переведены.


async def проверить_что_админ_останется(
    p, org_id: int, кто_просит: int, чья_строка: int, новая_роль: Optional[str] = None
) -> None:
    """Отказывает, если действие оставит организацию без администратора.

    ⚠️ ПИШЕТСЯ ОДИН РАЗ И ПЕРЕИСПОЛЬЗУЕТСЯ. Сегодня зовётся из отключения
    сотрудника, завтра — из смены роли (T104/T118): понижение админа
    до сотрудника это ровно то же снятие администратора, только другим
    путём. Дублировать проверку значило бы починить одну дверь из двух.

    ⚠️ С 31.08.2026 СЕБЯ СНЯТЬ МОЖНО, ЕСЛИ ОСТАЁТСЯ ДРУГОЙ АДМИНИСТРАТОР —
    решение владельца. Условие у себя и у чужого одно: организация не должна
    остаться без администратора.

    ⚠️ ЗАЧЕМ ВООБЩЕ. Замер 31.08.2026: `deactivate_user` проверял только
    роль просящего и организацию. Ни «это не ты сам», ни «останется ли
    хоть один админ». Админ мог отключить СЕБЯ — и организация становилась
    неуправляемой: ни завести сотрудника, ни создать приглашение,
    ни вернуть себе роль. **А после T115 отключённый не может даже войти**,
    чтобы попробовать. Тупик без выхода, чинится только руками в базе.

    `новая_роль=None` — строку отключают. Иначе — меняют роль на указанную.
    """
    # ⚠️ БЕЗУСЛОВНЫЙ ЗАПРЕТ НА СЕБЯ СНЯТ 31.08.2026, РЕШЕНИЕМ ВЛАДЕЛЬЦА.
    # Прежний довод был верен ровно при одном администраторе: снявший себя
    # не может отменить своё же действие, потому что доступа у него уже нет.
    # **При двух администраторах довод отпадает — отменить может второй.**
    # Владелец закрыл край политикой: в организации всегда минимум два
    # администратора (правило в `docs/RULES-FRONTEND.md`), и «передать дела
    # и уйти» обязано работать, иначе уйти нельзя вообще.
    #
    # ⚠️ ОТДЕЛЬНОЙ ПРОВЕРКИ НА СЕБЯ БОЛЬШЕ НЕТ, И ЭТО НЕ УПУЩЕНИЕ: у себя и
    # у чужого условие ОДНО И ТО ЖЕ — «останется ли в организации живой
    # администратор». Считать его дважды разными путями значило бы завести
    # две двери и однажды починить одну.

    цель = await p.fetchrow(
        "SELECT role, is_active FROM users WHERE id=$1 AND org_id=$2",
        чья_строка,
        org_id,
    )
    if not цель:
        raise HTTPException(status_code=404, detail="Not found")

    # Уходит ли из организации ЕЩЁ ОДИН активный админ?
    снимаем_админа = (
        цель["role"] == "admin"
        and цель["is_active"]
        and (новая_роль is None or новая_роль != "admin")
    )
    if not снимаем_админа:
        return

    осталось = await p.fetchval(
        "SELECT count(*) FROM users "
        "WHERE org_id=$1 AND role='admin' AND is_active=true AND id<>$2",
        org_id,
        чья_строка,
    )
    if осталось == 0:
        # ⚠️ ТЕКСТ РАЗНЫЙ, ПОТОМУ ЧТО РАЗНЫЙ СЛЕДУЮЩИЙ ШАГ. Замер 31.08.2026:
        # при одном админе первой срабатывала проверка «нельзя себя», и человек
        # читал, ЧТО нельзя, — ни почему, ни что делать. Формулировка с причиной
        # существовала, но до неё дело не доходило никогда, потому что стояла
        # второй. Теперь проверка одна, а объяснение подбирается по тому, себя
        # снимают или другого.
        raise HTTPException(
            status_code=409,
            detail=(
                "Вы единственный администратор организации — сначала пригласите "
                "второго администратора, иначе организацию будет некому вести"
                if кто_просит == чья_строка
                else "Это последний администратор организации — сначала назначьте другого"
            ),
        )


@router.patch("/{id}")
async def update_user(id: int, u: UserUpdate, user: dict = Depends(get_current_user)):
    # S-29: правит ФИО, email, ИНН, регион и табельный номер КОЛЛЕГИ — это
    # чужие персональные данные, а не собственный профиль (для себя есть
    # PATCH /me без гейта).
    _require_admin(user)
    fields = {
        k: v for k, v in u.model_dump(exclude_unset=True).items() if k in UPDATABLE
    }
    if not fields:
        raise HTTPException(status_code=400, detail="No fields to update")
    sets, values = собрать_set(fields, UPDATABLE)
    p = await get_pool()
    # ⚠️ ПОНИЖЕНИЕ АДМИНА — ЭТО СНЯТИЕ АДМИНИСТРАТОРА, ТОЛЬКО ДРУГОЙ ДВЕРЬЮ.
    # Гейт writes один раз и зовётся отсюда ровно за этим (T119 предвидела
    # T118 и оставила параметр `новая_роль`). Без этого вызова организация
    # теряла бы последнего админа через PATCH при закрытой двери DELETE —
    # чинить пришлось бы руками в базе.
    if "role" in fields:
        await проверить_что_админ_останется(
            p, user["org_id"], user["id"], id, новая_роль=fields["role"]
        )
    row = await p.fetchrow(
        f"UPDATE users SET {sets} WHERE id = ${len(values) + 1} "
        f"AND org_id = ${len(values) + 2} RETURNING *",
        *values,
        id,
        user["org_id"],
    )
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    return _safe(row)


@router.post("/{id}/restore")
async def restore_user(id: int, user: dict = Depends(get_current_user)):
    """Возвращает погашенного сотрудника в строй.

    ⚠️ ОТДЕЛЬНАЯ РУЧКА, А НЕ ПОЛЕ В PATCH. Положить `is_active` в `UPDATABLE`
    значило бы открыть ВТОРУЮ дверь к гашению — ту, что идёт мимо
    `проверить_что_админ_останется`, и организацию снова можно было бы
    оставить без администратора одним PATCH. Возврат безопасен в одну
    сторону: он админов не убавляет, поэтому гейт ему не нужен.

    ⚠️ ЗАЧЕМ ВООБЩЕ (T118/④, 31.08.2026): обратного действия не было НИ
    ОДНОГО — ни ручки `SET is_active = true` во всём `app/routers/`, ни поля
    в PATCH. Ошибочный свайп чинился только руками в базе.
    """
    _require_admin(user)
    p = await get_pool()
    row = await p.fetchrow(
        "UPDATE users SET is_active = true WHERE id = $1 AND org_id=$2 RETURNING *",
        id,
        user["org_id"],
    )
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    return _safe(row)


@router.delete("/{id}")
async def deactivate_user(id: int, user: dict = Depends(get_current_user)):
    """Soft-delete within the caller's org: keep the row, flip is_active off.

    ТОЛЬКО admin (S-29): без гейта рядовой сотрудник отключал кого угодно,
    включая администратора — то есть отбирал доступ у владельца.
    """
    _require_admin(user)
    p = await get_pool()
    # ⚠️ ОТКАЗ ДО ЗАПИСИ, а не после: проверка читает ту же организацию
    # и падает исключением, если действие оставит её без администратора.
    await проверить_что_админ_останется(p, user["org_id"], user["id"], id)
    await p.execute(
        "UPDATE users SET is_active = false WHERE id = $1 AND org_id=$2",
        id,
        user["org_id"],
    )
    return {"ok": True}
