"""Вход, сессии и смена пароля (задача S-31, часть 2).

КОНТУР, КОТОРЫЙ НЕ ВЫПОЛНЯЛСЯ В ТЕСТАХ НИ РАЗУ: login, refresh, logout,
/auth/me, /users/me и смена пароля. Замер покрытия 07.08.2026 показал по
ним честные нули — при том, что здесь живут блокировка после пяти попыток,
защита от энумерации логинов и отзыв сессии.

ОБЕ ПОЛОВИНЫ У КАЖДОЙ ПРОВЕРКИ (правило в CLAUDE.md): не только код
ответа, но и «токен не выдан», «счётчик попыток сдвинулся», «пароль
в базе не изменился». Код ответа сам по себе не отличает отказ от отказа,
после которого побочный эффект всё-таки случился.

ЧЕГО ЗДЕСЬ НЕТ, ЧЕСТНО. Ограничитель частоты входа (`@limiter.limit`
10/минуту) в этих тестах ВЫКЛЮЧЕН: счётчик у slowapi общий на процесс,
и десятый вход в прогоне ронял бы соседние тесты по причине, не связанной
с их предметом. Значит сам ограничитель остаётся непроверенным —
записано в S-31.
"""

import hashlib
from datetime import datetime, timedelta, timezone

import pytest

import app.routers.auth as auth_router
from app.auth import (
    create_access_token,
    create_refresh_token,
    get_current_user,
    hash_password,
    verify_password,
)
from app.main import app

ПАРОЛЬ = "правильный-пароль"
# bcrypt стоит 12 — считаем хеш ОДИН раз на модуль, а не в каждой фикстуре.
ХЕШ = hash_password(ПАРОЛЬ)
ЕДИНОЕ_СООБЩЕНИЕ = "Неверный логин или пароль"


@pytest.fixture(autouse=True)
def без_ограничителя():
    """Ограничитель частоты отключён, см. шапку модуля."""
    auth_router.limiter.enabled = False
    yield
    auth_router.limiter.enabled = True


@pytest.fixture
def я_сотрудник(сотрудник):
    """get_current_user возвращает НАСТОЯЩУЮ строку сотрудника.

    Нужна ручкам «про себя» (/users/me, смена пароля): они сверяют старый
    пароль с password_hash из зависимости, а стандартная подмена кладёт туда
    None — тогда «неверный пароль» получался бы по причине, не имеющей
    отношения к проверяемому.
    """
    app.dependency_overrides[get_current_user] = lambda: dict(сотрудник)
    return сотрудник


@pytest.fixture
def сотрудник(db):
    db.organizations.append(
        dict(id=1, name="ООО Ромашка", inn="7700000000", type="company")
    )
    row = dict(
        id=7,
        first_name="Иван",
        last_name="Петров",
        email="ivan@example.com",
        phone="+79990000000",
        password_hash=ХЕШ,
        role="employee",
        org_id=1,
        is_active=True,
        is_email_verified=True,
        failed_attempts=0,
        locked_until=None,
    )
    db.users.append(row)
    db._uid = 7
    return row


async def _вход(client, пароль, логин="ivan@example.com"):
    return await client.post(
        "/api/auth/login", json={"phone_or_email": логин, "password": пароль}
    )


# ──────────────────────────────── вход ──────────────────────────────────────


@pytest.mark.asyncio
async def test_login_success(client, сотрудник):
    r = await _вход(client, ПАРОЛЬ)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["access_token"] and d["refresh_token"]
    assert d["user"]["email"] == "ivan@example.com"
    assert "password_hash" not in d["user"], "хеш пароля не должен уезжать клиенту"
    assert d["organization"]["name"] == "ООО Ромашка"


@pytest.mark.asyncio
async def test_wrong_password_counts_attempt_and_issues_nothing(client, сотрудник):
    r = await _вход(client, "мимо")
    assert r.status_code == 401, r.text
    assert r.json()["detail"] == ЕДИНОЕ_СООБЩЕНИЕ
    assert "access_token" not in r.json()
    assert сотрудник["failed_attempts"] == 1, "неудачная попытка обязана считаться"


@pytest.mark.asyncio
async def test_unknown_login_answers_the_same_as_wrong_password(client, сотрудник):
    """Защита от перебора логинов: ответы обязаны быть НЕОТЛИЧИМЫ.

    Разные тексты («пользователь не найден» против «неверный пароль»)
    превращают форму входа в справочник действующих адресов.
    """
    чужой = await _вход(client, ПАРОЛЬ, логин="nikogo@example.com")
    свой = await _вход(client, "мимо")
    assert чужой.status_code == свой.status_code == 401
    assert чужой.json()["detail"] == свой.json()["detail"] == ЕДИНОЕ_СООБЩЕНИЕ


@pytest.mark.asyncio
async def test_fifth_attempt_locks_the_account(client, сотрудник):
    for _ in range(4):
        assert (await _вход(client, "мимо")).status_code == 401
    пятая = await _вход(client, "мимо")
    assert пятая.status_code == 429, пятая.text
    assert сотрудник["failed_attempts"] == 5
    assert сотрудник["locked_until"] is not None, "блокировка обязана проставиться"


@pytest.mark.asyncio
async def test_locked_account_rejects_even_the_correct_password(client, сотрудник):
    """Главная половина блокировки: с ВЕРНЫМ паролем внутрь тоже нельзя."""
    сотрудник["locked_until"] = datetime.now(timezone.utc) + timedelta(minutes=15)
    r = await _вход(client, ПАРОЛЬ)
    assert r.status_code == 429, r.text
    assert "access_token" not in r.json(), "во время блокировки токен не выдаётся"


@pytest.mark.asyncio
async def test_отключённый_сотрудник_получает_внятный_отказ(client, сотрудник):
    """Погашенный сотрудник отвергается НА ВХОДЕ, а не молча на первом запросе.

    ⚠️ ЗАМЕР ПРОДА 31.08.2026: трое сотрудников «ООО АОЦГ» стоят
    is_active=false. Вход их НЕ ПРОВЕРЯЛ — смотрел только пароль, выдавал
    токены и отвечал «вход выполнен». А `get_current_user` читает
    `AND is_active=true` и отвергал КАЖДЫЙ следующий запрос. Человек видел
    успешный вход и пустое приложение без единой причины.

    ⚠️ Доступа к данным у него не было — это не дыра, это ТИХИЙ ТУПИК,
    и он хуже честного отказа: непонятно, что чинить.
    """
    сотрудник["is_active"] = False
    r = await _вход(client, ПАРОЛЬ)
    assert r.status_code == 403, r.text
    тело = r.json()
    # код — для интерфейса, текст — для человека; ветвиться по тексту нельзя
    assert тело["code"] == "account_disabled"
    assert "отключена" in тело["detail"]
    # ⚠️ И ГЛАВНОЕ: токенов не выдано. Иначе «вход выполнен» остаётся правдой
    # для клиента, и тупик просто переезжает на шаг дальше.
    assert "access_token" not in тело
    assert "refresh_token" not in тело


@pytest.mark.asyncio
async def test_unverified_email_cannot_log_in(client, сотрудник):
    сотрудник["is_email_verified"] = False
    r = await _вход(client, ПАРОЛЬ)
    assert r.status_code == 403, r.text
    assert "access_token" not in r.json()


@pytest.mark.asyncio
async def test_expired_lock_forgives_the_counter(client, сотрудник):
    """ГЛАВНЫЙ сценарий S-59, которого не было в матрице до 20.08.2026.

    Отсидел 15 минут — прощён. До правки время снимало ЗАПРЕТ, но не ВИНУ:
    счётчик оставался на 5, и ОДНА следующая опечатка снова давала
    attempts >= 5 → мгновенные новые 15 минут. Человек без пароля
    и без восстановления (S-56) оставался в петле навсегда.
    Живой случай 18.08.2026: 10 накопленных попыток, вход невозможен часами.
    """
    for _ in range(4):
        assert (await _вход(client, "мимо")).status_code == 401
    assert (await _вход(client, "мимо")).status_code == 429
    assert сотрудник["failed_attempts"] == 5

    # время вышло
    сотрудник["locked_until"] = datetime.now(timezone.utc) - timedelta(seconds=1)

    ответ = await _вход(client, "мимо")
    assert ответ.status_code == 401, (
        "после истёкшей блокировки ОДНА ошибка обязана дать 401, а не 429: "
        f"получили {ответ.status_code}"
    )
    assert сотрудник["failed_attempts"] == 1, (
        "счётчик обязан начаться заново, а не продолжиться с пятого"
    )
    assert сотрудник["locked_until"] is None


@pytest.mark.asyncio
async def test_correct_password_resets_counter_even_if_email_unverified(
    client, сотрудник
):
    """Счётчик защищает от ПОДБОРА, а верный пароль доказывает, что подбора нет.

    До правки 403 «Подтвердите email» вылетал РАНЬШЕ сброса, и человек
    с неподтверждённой почтой копил попытки при каждом верном вводе —
    то есть блокировался, вводя правильный пароль.
    """
    сотрудник["is_email_verified"] = False
    сотрудник["failed_attempts"] = 3

    ответ = await _вход(client, ПАРОЛЬ)
    assert ответ.status_code == 403, ответ.text
    assert "access_token" not in ответ.json()
    assert сотрудник["failed_attempts"] == 0, (
        "верный пароль обязан обнулить счётчик ДО ворот подтверждения почты"
    )
    assert сотрудник["locked_until"] is None


@pytest.mark.asyncio
async def test_successful_login_resets_the_counter(client, сотрудник):
    сотрудник["failed_attempts"] = 3
    assert (await _вход(client, ПАРОЛЬ)).status_code == 200
    assert сотрудник["failed_attempts"] == 0 and сотрудник["locked_until"] is None


# ─────────────────────────── обновление сессии ──────────────────────────────


@pytest.mark.asyncio
async def test_refresh_returns_new_access(client, сотрудник):
    rt = create_refresh_token(сотрудник["id"])
    r = await client.post("/api/auth/refresh", json={"refresh_token": rt})
    assert r.status_code == 200, r.text
    assert r.json()["access_token"]


@pytest.mark.asyncio
async def test_access_token_is_not_accepted_as_refresh(client, сотрудник):
    """Тип токена проверяется: access вместо refresh — не сессия."""
    r = await client.post(
        "/api/auth/refresh",
        json={"refresh_token": create_access_token(сотрудник["id"])},
    )
    assert r.status_code == 401, r.text
    assert "access_token" not in r.json()


@pytest.mark.asyncio
async def test_revoked_refresh_is_dead(client, сотрудник):
    rt = create_refresh_token(сотрудник["id"])
    # Отзыв делаем ЧЕРЕЗ РУЧКУ выхода, а не руками в таблице: иначе тест
    # проверял бы только SELECT, а не связку «выход → сессия мертва».
    выход = await client.post("/api/auth/logout", json={"refresh_token": rt})
    assert выход.status_code == 200, выход.text
    r = await client.post("/api/auth/refresh", json={"refresh_token": rt})
    assert r.status_code == 401, r.text
    assert "access_token" not in r.json()


@pytest.mark.asyncio
async def test_logout_writes_hash_not_the_token(client, db, сотрудник):
    """В таблицу отзыва ложится ХЕШ: сам токен там хранить нельзя."""
    rt = create_refresh_token(сотрудник["id"])
    await client.post("/api/auth/logout", json={"refresh_token": rt})
    (запись,) = db.revoked_tokens
    assert запись["token_hash"] == hashlib.sha256(rt.encode()).hexdigest()
    assert rt not in запись["token_hash"]


@pytest.mark.asyncio
async def test_deactivated_user_cannot_refresh(client, сотрудник):
    """Отключили человека — живой refresh-токен перестаёт работать."""
    rt = create_refresh_token(сотрудник["id"])
    сотрудник["is_active"] = False
    r = await client.post("/api/auth/refresh", json={"refresh_token": rt})
    assert r.status_code == 401, r.text
    assert "access_token" not in r.json()


# ──────────────────────── профиль и смена пароля ────────────────────────────


@pytest.mark.asyncio
async def test_me_hides_secrets(client, сотрудник):
    r = await client.get("/api/auth/me")
    assert r.status_code == 200, r.text
    assert "password_hash" not in r.json()["user"]
    assert "email_verify_token" not in r.json()["user"]


@pytest.mark.asyncio
async def test_change_password_requires_the_old_one(client, я_сотрудник):
    r = await client.post(
        "/api/users/me/change-password",
        json={"old_password": "не тот", "new_password": "новый-длинный-пароль"},
    )
    assert r.status_code == 400, r.text
    assert я_сотрудник["password_hash"] == ХЕШ, "пароль не должен был измениться"


@pytest.mark.asyncio
async def test_change_password_rejects_short_new(client, я_сотрудник):
    r = await client.post(
        "/api/users/me/change-password",
        json={"old_password": ПАРОЛЬ, "new_password": "1234"},
    )
    assert r.status_code == 400, r.text
    assert я_сотрудник["password_hash"] == ХЕШ


@pytest.mark.asyncio
async def test_change_password_replaces_the_hash(client, я_сотрудник):
    r = await client.post(
        "/api/users/me/change-password",
        json={"old_password": ПАРОЛЬ, "new_password": "новый-длинный-пароль"},
    )
    assert r.status_code == 200, r.text
    assert я_сотрудник["password_hash"] != ХЕШ
    assert verify_password("новый-длинный-пароль", я_сотрудник["password_hash"])
    assert not verify_password(ПАРОЛЬ, я_сотрудник["password_hash"]), (
        "старый пароль обязан перестать подходить"
    )


@pytest.mark.asyncio
async def test_patch_me_touches_only_whitelisted_columns(client, я_сотрудник):
    """Свой профиль правится, но роль и почта через эту ручку — нет."""
    r = await client.patch(
        "/api/users/me",
        json={
            "first_name": "Иоанн",
            "role": "admin",
            "email": "hacker@example.com",
            "inn": "000000000000",
        },
    )
    assert r.status_code == 200, r.text
    assert я_сотрудник["first_name"] == "Иоанн", "своё имя менять можно"
    assert я_сотрудник["role"] == "employee", "роль через /me не меняется"
    assert я_сотрудник["email"] == "ivan@example.com", "почта через /me не меняется"
    assert я_сотрудник.get("inn") != "000000000000", "ИНН через /me не меняется"


@pytest.mark.asyncio
async def test_get_me_returns_profile_without_secrets(client, db, я_сотрудник):
    """Свой профиль: то, что нужно интерфейсу, и ничего лишнего.

    Ручка отдаёт не строку таблицы, а собранный ответ (_me_payload),
    поэтому проверяется и наличие нужных полей, и отсутствие секретов —
    пустой словарь прошёл бы половину проверок.
    """
    db.consents.append(
        dict(
            id=1,
            user_id=str(я_сотрудник["id"]),
            consent_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
            policy_version="1.0",
        )
    )
    r = await client.get("/api/users/me")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["id"] == я_сотрудник["id"] and d["email"] == "ivan@example.com"
    assert d["role"] == "employee"
    assert d["consent"]["policy_version"] == "1.0", "согласие 152-ФЗ подтягивается"
    assert "password_hash" not in d and "email_verify_token" not in d


@pytest.mark.asyncio
async def test_login_rate_limit_cuts_off_the_eleventh(client, сотрудник):
    """Ограничитель частоты входа: 10 в минуту с одного адреса.

    СЧЁТЧИК ИЗОЛИРУЕТСЯ, а не обходится: slowapi держит его в памяти
    процесса и умеет `reset()`, поэтому тест включает ограничитель,
    обнуляет счётчик до и после — и порядок тестов перестаёт что-либо
    значить. Именно из-за общего счётчика в остальных тестах модуля
    ограничитель выключен (см. шапку).

    Логин берётся НЕСУЩЕСТВУЮЩИЙ: у живого аккаунта пятая неудача даёт
    свой 429 (блокировка), и два разных 429 стали бы неразличимы —
    тест доказывал бы не то.
    """
    auth_router.limiter.enabled = True
    auth_router.limiter.reset()
    try:
        коды = [
            (await _вход(client, "мимо", логин="nikogo@example.com")).status_code
            for _ in range(11)
        ]
    finally:
        auth_router.limiter.reset()
        auth_router.limiter.enabled = False

    assert коды[:10] == [401] * 10, f"первые десять попыток должны доходить: {коды}"
    assert коды[10] == 429, f"одиннадцатую обязан срезать ограничитель: {коды}"
    assert сотрудник["failed_attempts"] == 0, (
        "перебор чужого логина не должен трогать счётчик живого аккаунта"
    )
