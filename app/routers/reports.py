from typing import List, Literal, Optional

import asyncpg
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel

from app.auth import can_see_all, get_current_user
from app.database import get_pool
from app.email_service import send_report_status_email, send_report_submitted_email
from app.notifications import (
    ОТЧЁТ_НА_ПРОВЕРКУ,
    ОТЧЁТ_ОДОБРЕН,
    ОТЧЁТ_ОТКЛОНЁН,
    кому_управляющим,
    почты as почты_адресатов,
    записать as записать_событие,
)
from app.routers.auth import APP_URL

# ⚠️ ИМПОРТ, А НЕ КОПИЯ КОРТЕЖА (REP-EXPDEL): набор «живых» исходов выгрузки
# и срок, после которого `running` считается прервавшейся, — один на всю
# платформу. Вторая копия здесь разошлась бы с первой молча, и удаление
# начало бы пускать то, что выгрузка считает живым.
from app.routers.odata import ЖИВЫЕ, ПРЕРВАНА_ЧЕРЕЗ_МИНУТ
from app.routers.receipts import с_признаком_дозапроса

router = APIRouter(prefix="/api/reports", tags=["reports"])

# Статусы, в которых отчёт разрешено ИЗМЕНЯТЬ: удалять (ЧП2) и править состав
# (ЧП3). «На проверке» и «Одобрен» заморожены — это уже поданный либо принятый
# к учёту документ, менять его за спиной проверяющего нельзя.
EDITABLE_STATUSES = ("Черновик", "Отклонён")


# REP-ROLES: утверждающие статусы. Перевод в них — управленческое решение
# с денежными последствиями (возмещение сотруднику), поэтому доступен только
# бухгалтеру и админу. Автор своё отправляет («На проверке») и отзывает
# («Черновик») сам, но не утверждает — это контроль расходов, а не
# самообслуживание. Образец гейта — _require_category_manager в categories.
APPROVAL_STATUSES = ("Одобрен", "Отклонён")


def _require_approver(user: dict) -> None:
    """Одобрить/отклонить отчёт может только бухгалтер или администратор.

    Проверяем ДО обращения к БД: сообщение говорит о правах действующего лица
    и ничего не сообщает о существовании отчёта, так что 403 здесь ничего не
    раскрывает (видимость чужих отчётов закрыта отдельно, в REP-ACL).
    """
    if not can_see_all(user["role"]):
        raise HTTPException(
            status_code=403,
            detail="Одобрять и отклонять отчёты может только бухгалтер или администратор",
        )


def _ensure_editable(status: str) -> None:
    """Единый гейт изменения отчёта — один на удаление и на правку состава.

    Тексты объясняют СЛЕДУЮЩИЙ ШАГ, а не просто запрещают: пользователь должен
    понять, что делать дальше, не читая документацию.
    """
    if status in EDITABLE_STATUSES:
        return
    if status == "На проверке":
        raise HTTPException(
            status_code=409,
            detail="Отчёт на проверке — сначала отзовите его, потом изменяйте",
        )
    # «Одобрен»: состав и удаление закрыты здесь. Снять одобрение можно —
    # переводом статуса в PATCH, и ТОЛЬКО бухгалтером или админом (гейт
    # «откуда» в update_status, 04.09.2026). Раньше на этом месте стояло
    # обещание будущей задачи REP-UNAPPROVE, а снятие уже работало и было
    # доступно автору-сотруднику: комментарий описывал не тот код, что рядом.
    raise HTTPException(
        status_code=409,
        detail="Одобренный отчёт изменить нельзя: он принят к учёту",
    )


# ── REP-EXPDEL: УДАЛЕНИЕ ОТЧЁТА. Свой гейт, не `_ensure_editable` ──────────
# Решения владельца 22.09.2026: В1 — удаляются ВСЕ ЧЕТЫРЕ статуса, включая
# выгружавшийся в 1С; В2 — сотрудник удаляет СВОЙ ЧЕРНОВИК И СВОЙ ОТКЛОНЁННЫЙ,
# всё, что лежит у бухгалтера или принято к учёту, сносит тот, кто отвечает
# за учёт. «Правку состава» и «снос целиком» раньше решала одна функция,
# и удаление оказалось заперто вместе с правкой: обходили снятием одобрения.
#
# ⚠️ «ОТКЛОНЁН» ЗДЕСЬ — ПОПРАВКА ВЛАДЕЛЬЦА 22.09.2026 К ПЕРВОЙ РЕДАКЦИИ ЭТОГО
# ЗАХОДА, И ЗАПИСАНА ОНА, ЧТОБЫ НЕ СУЗИЛИ СНОВА. Первая редакция оставляла
# сотруднику один черновик — буквально по тексту решения, — и это отняло бы
# сегодняшнюю возможность: отчёт вернули на доработку, человек хочет снести
# его и завести заново. Набор совпадает с EDITABLE_STATUSES не случайно: то,
# что сотрудник вправе ПРАВИТЬ, он вправе и удалить.
СОТРУДНИК_УДАЛЯЕТ = ("Черновик", "Отклонён")


def _ensure_can_delete(status: str, user: dict) -> None:
    """Кто может удалить отчёт в этом статусе (В2).

    Текст отказа говорит, К КОМУ ИДТИ, а не просто «нельзя»: отчёт уже
    у бухгалтера либо принят к учёту, и решение о сносе принимает он.
    Статус в тексте не раскрывает лишнего: сотрудник видит только свои
    отчёты, чужой для него неотличим от несуществующего (404 выше).
    """
    if status in СОТРУДНИК_УДАЛЯЕТ:
        return
    if not can_see_all(user["role"]):
        raise HTTPException(
            status_code=403,
            detail="Отчёт в статусе «%s» удаляет бухгалтер или администратор" % status,
        )


def _ensure_can_rename(status: str, user: dict) -> None:
    """Кто может переименовать отчёт в этом статусе (REP-RENAME, 23.09.2026).

    ⚠️ НАБОР СТАТУСОВ ТОТ ЖЕ, ЧТО У УДАЛЕНИЯ, И ЭТО НЕ СОВПАДЕНИЕ: и то
    и другое — правка ЧУЖОГО ГЛАЗАМИ документа. Пока отчёт не показан
    бухгалтеру, он целиком свой; показан или принят к учёту — менять его
    имя за спиной проверяющего нельзя, это делает тот, кто за учёт отвечает.
    Отдельная константа здесь завела бы вторую копию правила, которая
    разошлась бы с первой молча.
    """
    if status in СОТРУДНИК_УДАЛЯЕТ:
        return
    if not can_see_all(user["role"]):
        raise HTTPException(
            status_code=403,
            detail="Отчёт в статусе «%s» переименовывает бухгалтер или администратор"
            % status,
        )


async def _fetch_report_for_update(conn, id: int, user: dict):
    """Тот же отбор, что `_fetch_report`, но строка берётся под замок.

    Замок нужен именно удалению: без него между проверкой статуса и самим
    удалением успевает пройти чужая смена статуса или второе удаление, и
    проверка окажется про прошлое состояние. `FOR UPDATE` заставляет второго
    ждать конца транзакции, а не проскакивать мимо.
    """
    if can_see_all(user["role"]):
        return await conn.fetchrow(
            "SELECT * FROM reports WHERE id=$1 AND org_id=$2 FOR UPDATE",
            id,
            user["org_id"],
        )
    return await conn.fetchrow(
        "SELECT * FROM reports WHERE id=$1 AND org_id=$2 AND user_id=$3 FOR UPDATE",
        id,
        user["org_id"],
        user["id"],
    )


async def _ensure_no_live_export(
    conn, id: int, org_id: int, действие="удаляйте"
) -> None:
    """Отчёт с живой отправкой в 1С удаляется только после отмены (В3).

    ⚠️ ПОЧЕМУ НЕ ОТМЕНЯТЬ САМИ, ХОТЯ ЭТО БЫЛО БЫ УДОБНЕЕ. Отмена у нас значит
    «идите в 1С и пометьте документ на удаление руками»: платформа документ
    в базе клиента не трогает никогда и не может. Спрятав отмену внутрь
    удаления, мы спрятали бы и это обязательство — наш отчёт исчез бы,
    документ в чужом учёте остался, и человек об этом не узнал бы. Отказ
    заставляет прочитать и нажать отмену осознанно.

    ⚠️ ПРЕРВАННАЯ ОТПРАВКА — ТОТ ЖЕ ПУТЬ, РУКАМИ, автоматики нет (решение
    владельца по 1C-29): создан ли документ в 1С, неизвестно, и решить это
    может только человек, посмотрев в 1С.
    """
    живая = await conn.fetchrow(
        "SELECT outcome,"
        " (outcome = 'running' AND started_at < NOW() - make_interval(mins => $3))"
        " AS прервана"
        " FROM odata_exports"
        " WHERE report_id=$1 AND org_id=$2 AND outcome = ANY($4::text[])"
        " ORDER BY started_at DESC, id DESC LIMIT 1",
        id,
        org_id,
        ПРЕРВАНА_ЧЕРЕЗ_МИНУТ,
        list(ЖИВЫЕ),
    )
    if not живая:
        return
    if живая["прервана"]:
        что = "Отправка отчёта в 1С прервалась"
    elif живая["outcome"] == "running":
        что = "Отчёт отправляется в 1С"
    else:
        что = "Отчёт отправлен в 1С"
    raise HTTPException(
        status_code=409,
        detail="%s. Сначала отмените отправку, потом %s" % (что, действие),
    )


class ReportIn(BaseModel):
    # total НЕ принимаем: он производный от состава (см. _recalc_total).
    # Раньше сумму присылал клиент, и после любого изменения состава она
    # протухала. Лишнее поле в запросе Pydantic просто игнорирует.
    title: str
    receiptIds: List[int]


class StatusIn(BaseModel):
    # Жизненный цикл отчёта: Черновик → На проверке → Одобрен / Отклонён.
    # Literal закрывает дыру — PATCH больше не примет произвольную строку статуса.
    status: Literal["Черновик", "На проверке", "Одобрен", "Отклонён"]
    # ⚠️ ПРИЧИНА ОТКАЗА ОБЯЗАТЕЛЬНА ПРИ «Отклонён» (T159, требование владельца
    # 04.09.2026): «без причины человек всё равно идёт выяснять, и уведомление
    # не экономит ему ничего». Проверка ниже в обработчике, а не в модели:
    # ответ должен быть человеческим 400 с объяснением, а не 422 со схемой.
    reason: Optional[str] = None


class TitleIn(BaseModel):
    # ⚠️ БЕЗ ОГРАНИЧЕНИЙ PYDANTIC НАМЕРЕННО. Схема ответила бы 422 своими
    # словами про «string_too_long», а человеку нужен текст про отчёт.
    # Проверка — в обработчике, ответ — 422 с нашим объяснением.
    title: str


class ReceiptsIn(BaseModel):
    # Список, а не один id: «добавить выбранные» из шторки — тот же вызов,
    # что и «прикрепить этот чек» ([rid]).
    receiptIds: List[int]


# Один чек живёт ровно в одном отчёте (uq_report_items_receipt_id). Нарушение
# приходит из БД как UniqueViolationError — переводим в понятный 409, чтобы
# клиент показал текст, а не «500 Internal Server Error».
RECEIPT_TAKEN = "Чек уже в другом отчёте"

# Инвариант АО-1: авансовый отчёт закрывает подотчёт КОНКРЕТНОГО лица, поэтому
# все его чеки принадлежат одному сотруднику — автору отчёта. Смешанный отчёт
# невозможно провести: непонятно, кому возмещать. Бухгалтер видит все чеки орг
# (can_see_all) и технически мог бы собрать «солянку» — это и запрещаем.
# «Собрать отчёт ЗА сотрудника» появится явной фичей REP-ONBEHALF.
RECEIPT_FOREIGN = "Чек другого сотрудника — соберите отдельный отчёт"
# receipts.user_id остался nullable с A-ACL (легаси; на проде таких строк нет).
# «Ничей» чек ломал бы инвариант, поэтому в отчёт не пускаем.
RECEIPT_NO_OWNER = "У чека нет владельца — его нельзя включить в отчёт"


def _ensure_same_owner(rows, author_id: int) -> None:
    """Все чеки принадлежат подотчётному лицу отчёта. rows — те же строки, что
    уже прочитаны IDOR-проверкой, второго обращения к БД не делаем."""
    for row in rows:
        if row["user_id"] is None:
            raise HTTPException(status_code=409, detail=RECEIPT_NO_OWNER)
        if row["user_id"] != author_id:
            raise HTTPException(status_code=409, detail=RECEIPT_FOREIGN)


async def _recalc_total(conn, report_id: int, org_id: int):
    """Пересчитать reports.total из фактического состава и вернуть строку отчёта.

    total — ПРОИЗВОДНОЕ значение, а не пользовательский ввод: клиент его не
    присылает, БД хранит как снимок для списка. Вызывать в ОДНОЙ транзакции с
    любым изменением состава (создание отчёта, добавление/удаление чека), иначе
    сумма разъедется с чеками. org_id внутри подзапроса — та же org-scope
    защита, что и везде: чужие чеки в сумму не попадут даже теоретически.
    """
    return await conn.fetchrow(
        """UPDATE reports SET total = COALESCE((
               SELECT SUM(rc.amount)
               FROM report_items ri
               JOIN receipts rc ON rc.id = ri.receipt_id
               WHERE ri.report_id = reports.id AND rc.org_id = $2
           ), 0)
           WHERE id = $1 AND org_id = $2
           RETURNING *""",
        report_id,
        org_id,
    )


async def _в_1с(conn, org_id: int, report_id: int) -> bool:
    """Есть ли у отчёта ЖИВАЯ выгрузка в 1С — признак `in_1c` в ответах.

    ⚠️ ОДНО ИМЯ И ОДНО МЕСТО, ПОТОМУ ЧТО ФОРМ ОТВЕТА ПЯТЬ. Список, создание,
    чтение одного, смена статуса и общий сборщик строят словарь каждый сам,
    и признак, добавленный в одну форму, развалил бы договор «POST == PATCH ==
    элемент списка» — его стерегут тесты формы.
    """
    return bool(
        await conn.fetchval(
            "SELECT 1 FROM odata_exports"
            " WHERE org_id=$1 AND report_id=$2 AND outcome = ANY($3::text[]) LIMIT 1",
            org_id,
            report_id,
            list(ЖИВЫЕ),
        )
    )


async def _with_receipt_ids(conn, rep) -> dict:
    """Отчёт + receiptIds — форма элемента GET-списка (и ответа PATCH/POST)."""
    items = await conn.fetch(
        "SELECT receipt_id FROM report_items WHERE report_id=$1 ORDER BY receipt_id",
        rep["id"],
    )
    d = dict(rep)
    d["receiptIds"] = [i["receipt_id"] for i in items]
    d["in_1c"] = await _в_1с(conn, rep["org_id"], rep["id"])
    return d


async def _fetch_report(conn, id: int, user: dict):
    """Отчёт с учётом org-scope И author-scope (REP-ACL).

    Авансовый отчёт принадлежит подотчётному лицу, поэтому сотрудник видит
    только свои; бухгалтер и админ (can_see_all) — все в организации. Тот же
    приём, что в receipts. Возвращает None, если отчёта нет ИЛИ он недоступен —
    вызывающий отдаёт 404, чтобы чужой отчёт был неотличим от несуществующего.
    """
    if can_see_all(user["role"]):
        return await conn.fetchrow(
            "SELECT * FROM reports WHERE id=$1 AND org_id=$2", id, user["org_id"]
        )
    return await conn.fetchrow(
        "SELECT * FROM reports WHERE id=$1 AND org_id=$2 AND user_id=$3",
        id,
        user["org_id"],
        user["id"],
    )


async def _load_editable_report(conn, id: int, user: dict):
    """Доступный отчёт + проверка, что его вообще можно менять.

    404 для чужого/несуществующего (неотличимы), 409 с объяснением следующего
    шага для замороженных статусов. Используется обеими ручками состава.
    """
    rep = await _fetch_report(conn, id, user)
    if not rep:
        raise HTTPException(status_code=404, detail="Not found")
    _ensure_editable(rep["status"])
    return rep


@router.get("/")
async def get_reports(user: dict = Depends(get_current_user)):
    p = await get_pool()
    # REP-ACL: сотрудник видит только свои отчёты, бухгалтер/админ — все.
    if can_see_all(user["role"]):
        reports = await p.fetch(
            "SELECT * FROM reports WHERE org_id=$1 ORDER BY created DESC",
            user["org_id"],
        )
        items = await p.fetch(
            "SELECT ri.* FROM report_items ri JOIN reports r ON r.id = ri.report_id "
            "WHERE r.org_id=$1",
            user["org_id"],
        )
    else:
        reports = await p.fetch(
            "SELECT * FROM reports WHERE org_id=$1 AND user_id=$2 ORDER BY created DESC",
            user["org_id"],
            user["id"],
        )
        # Состав тянем тем же фильтром: чужие связи не должны даже читаться.
        items = await p.fetch(
            "SELECT ri.* FROM report_items ri JOIN reports r ON r.id = ri.report_id "
            "WHERE r.org_id=$1 AND r.user_id=$2",
            user["org_id"],
            user["id"],
        )
    # ⚠️ ПРИЗНАК «УЖЕ В 1С» — ОДНИМ ЗАПРОСОМ НА ВЕСЬ СПИСОК (REP-SWIPE1C).
    # Свайп «В 1С» показывается только там, где отправка ещё возможна, а знать
    # это списку неоткуда: ручка состояния отвечает ПРО ОДИН отчёт, и спрашивать
    # её построчно значило бы слать столько запросов, сколько строк на экране —
    # на главном экране приложения и при общем пределе 60 запросов в минуту.
    # Поэтому здесь один запрос по всем живым выгрузкам организации.
    #
    # ⚠️ ЭТО НЕ [[1C-03]] И ЕЁ НЕ ЗАКРЫВАЕТ: там нужны отметка и ДАТА выгрузки
    # на самом отчёте для машинного опроса «только новые», здесь — булев признак
    # для одной кнопки. Смежное, не то же самое.
    живые = {
        з["report_id"]
        for з in await p.fetch(
            "SELECT DISTINCT report_id FROM odata_exports"
            " WHERE org_id=$1 AND outcome = ANY($2::text[])",
            user["org_id"],
            list(ЖИВЫЕ),
        )
    }
    result = []
    for rep in reports:
        d = dict(rep)
        d["receiptIds"] = [
            i["receipt_id"] for i in items if i["report_id"] == rep["id"]
        ]
        # Имя как у чеков (`in_report`): признак принадлежности, тем же складом.
        d["in_1c"] = rep["id"] in живые
        result.append(d)
    return result


@router.post("/")
async def create_report(r: ReportIn, user: dict = Depends(get_current_user)):
    p = await get_pool()
    async with p.acquire() as conn:
        async with conn.transaction():
            # REP-AUTHOR: автор = создатель. Бухгалтеру «создать ЗА сотрудника»
            # пока нельзя — это отдельная явная фича REP-ONBEHALF с правом
            # и следом в audit log, а не побочный эффект отсутствия проверки.
            rep = await conn.fetchrow(
                "INSERT INTO reports (title,org_id,user_id) VALUES ($1,$2,$3) RETURNING *",
                r.title,
                user["org_id"],
                user["id"],
            )
            # IDOR-защита: все receiptIds обязаны принадлежать организации
            # пользователя. Проверяем ОДНИМ запросом, ДО вставок и внутри
            # транзакции — любой чужой/несуществующий id → 403 и откат всей
            # вставки (для финпродукта явная ошибка лучше тихого пропуска).
            if r.receiptIds:
                owned = await conn.fetch(
                    "SELECT id, user_id FROM receipts "
                    "WHERE id = ANY($1::int[]) AND org_id = $2",
                    r.receiptIds,
                    user["org_id"],
                )
                if {row["id"] for row in owned} != set(r.receiptIds):
                    # Обобщённый detail: НЕ перечисляем недоступные id, чтобы не
                    # подтверждать их существование (это тоже утечка).
                    raise HTTPException(
                        status_code=403, detail="Один или несколько чеков недоступны"
                    )
                # Автор отчёта = создатель, значит и все чеки — его.
                _ensure_same_owner(owned, user["id"])
                for rid in r.receiptIds:
                    try:
                        await conn.execute(
                            "INSERT INTO report_items VALUES ($1,$2)", rep["id"], rid
                        )
                    except asyncpg.UniqueViolationError:
                        # Чек уже лежит в другом отчёте — вся вставка откатится.
                        raise HTTPException(status_code=409, detail=RECEIPT_TAKEN)
            # total считаем ПОСЛЕ вставки состава, в той же транзакции.
            rep = await _recalc_total(conn, rep["id"], user["org_id"])
    d = dict(rep)
    d["receiptIds"] = r.receiptIds
    d["in_1c"] = False  # только что созданный отчёт в 1С не уезжал
    return d


@router.get("/{id}")
async def get_report(id: int, user: dict = Depends(get_current_user)):
    """Детали отчёта: сам отчёт + receiptIds + РАЗВЁРНУТЫЕ чеки.

    Чеки тянем тем же `SELECT *`, что и список GET /api/receipts/ — форма чека
    в деталях отчёта совпадает с формой в списке чеков по построению, а не по
    договорённости (отдельный список колонок разъехался бы при первой же новой
    колонке). Ролевой фильтр — тот же can_see_all: employee видит только свои
    чеки, поэтому массив receipts может быть короче receiptIds.
    """
    p = await get_pool()
    row = await _fetch_report(p, id, user)
    # Чужой отчёт неотличим от несуществующего — как в PATCH.
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    items = await p.fetch(
        "SELECT receipt_id FROM report_items WHERE report_id=$1 ORDER BY receipt_id",
        id,
    )
    ids = [i["receipt_id"] for i in items]
    receipts = []
    if ids:
        # in_report / report_id / report_title добавлены и здесь — иначе форма
        # чека в деталях отчёта разошлась бы с формой в списке чеков (её
        # сторожит контрактный тест). Значения тут предсказуемы: чек лежит
        # в ЭТОМ отчёте по построению.
        if can_see_all(user["role"]):
            receipts = await p.fetch(
                """SELECT receipts.*,
                          (ri.receipt_id IS NOT NULL) AS in_report,
                          rep.id    AS report_id,
                          rep.title AS report_title
                   FROM receipts
                   LEFT JOIN report_items ri ON ri.receipt_id = receipts.id
                   LEFT JOIN reports rep     ON rep.id = ri.report_id
                   WHERE receipts.id = ANY($1::int[]) AND receipts.org_id=$2
                   ORDER BY receipts.date DESC""",
                ids,
                user["org_id"],
            )
        else:
            receipts = await p.fetch(
                """SELECT receipts.*,
                          (ri.receipt_id IS NOT NULL) AS in_report,
                          rep.id    AS report_id,
                          rep.title AS report_title
                   FROM receipts
                   LEFT JOIN report_items ri ON ri.receipt_id = receipts.id
                   LEFT JOIN reports rep     ON rep.id = ri.report_id
                   WHERE receipts.id = ANY($1::int[]) AND receipts.org_id=$2
                     AND receipts.user_id=$3 ORDER BY receipts.date DESC""",
                ids,
                user["org_id"],
                user["id"],
            )
    d = dict(row)
    d["receiptIds"] = ids
    d["in_1c"] = await p.fetchval(
        "SELECT EXISTS (SELECT 1 FROM odata_exports WHERE org_id=$1"
        " AND report_id=$2 AND outcome = ANY($3::text[]))",
        user["org_id"],
        id,
        list(ЖИВЫЕ),
    )
    # ⚠️ ТА ЖЕ ФОРМА, ЧТО У СПИСКА ЧЕКОВ (T132). Сторож
    # `test_get_report_detail_has_list_fields_plus_receipts` требует совпадения,
    # и он прав: чек внутри отчёта — тот же чек, и кнопка дозапроса на нём
    # должна быть видна по тому же признаку.
    d["receipts"] = [с_признаком_дозапроса(r) for r in receipts]
    return d


# ⚠️ ДЛИНА НАЗВАНИЯ — 255, ПОТОМУ ЧТО СТОЛЬКО ДЕРЖИТ КОЛОНКА
# (`reports.title VARCHAR(255)`). Проверять надо ДО записи: без проверки
# длинное имя упало бы ошибкой базы, то есть человек получил бы 500 вместо
# слов — ровно тот класс, который чинили в [[REP-EXPDEL]].
ДЛИНА_НАЗВАНИЯ = 255


@router.patch("/{id}/title")
async def переименовать(id: int, r: TitleIn, user: dict = Depends(get_current_user)):
    """Переименовать отчёт (REP-RENAME, решение владельца 23.09.2026).

    ⚠️ ОТДЕЛЬНЫЙ ПУТЬ, А НЕ ПОЛЕ В `PATCH /{id}`. Тот PATCH принимает
    `StatusIn` со ОБЯЗАТЕЛЬНЫМ статусом и тянет за собой уведомления, письма
    и гейт утверждения. Название к этому отношения не имеет: смешав их, мы
    получили бы ручку, которая на переименование шлёт письма о смене статуса.

    ⚠️ ПОЧЕМУ ДО ОТПРАВКИ В 1С. Название уезжает В ДОКУМЕНТ 1С и остаётся
    снимком в журнале выгрузок ([[REP-EXPDEL]]): переименовав отчёт после
    отправки, мы развели бы наше имя и имя в чужой бухгалтерии — и человек,
    ищущий документ по названию, не нашёл бы его.

    ⚠️ ОДНА ТРАНЗАКЦИЯ И ЗАМОК, как у удаления: между проверкой статуса
    и записью статус может смениться.
    """
    название = (r.title or "").strip()
    if not название:
        raise HTTPException(
            status_code=422, detail="Название отчёта не может быть пустым"
        )
    if len(название) > ДЛИНА_НАЗВАНИЯ:
        raise HTTPException(
            status_code=422,
            detail="Название длиннее %d знаков — сократите" % ДЛИНА_НАЗВАНИЯ,
        )
    p = await get_pool()
    async with p.acquire() as conn:
        async with conn.transaction():
            row = await _fetch_report_for_update(conn, id, user)
            if not row:
                raise HTTPException(status_code=404, detail="Not found")
            _ensure_can_rename(row["status"], user)
            await _ensure_no_live_export(
                conn, id, user["org_id"], действие="переименовывайте"
            )
            обновлён = await conn.fetchrow(
                "UPDATE reports SET title=$1 WHERE id=$2 AND org_id=$3 RETURNING *",
                название,
                id,
                user["org_id"],
            )
            return await _with_receipt_ids(conn, обновлён)


@router.delete("/{id}", status_code=204)
async def delete_report(id: int, user: dict = Depends(get_current_user)):
    """Удалить отчёт — из ЛЮБОГО статуса (REP-EXPDEL, решение владельца 22.09.2026).

    Состав (report_items) уходит каскадом. Сами чеки НЕ трогаем: они просто
    освобождаются и снова доступны для другого отчёта. Связь с журналом
    выгрузок обнуляется правилом базы, снимки номера и названия остаются.

    ⚠️ ЗДЕСЬ НЕ `_ensure_editable`, И ЭТО НАМЕРЕННО. Тот гейт отвечает на
    вопрос «можно ли ПРАВИТЬ состав» и пускает только черновик с отклонённым;
    удаление — другой вопрос: одобренный отчёт править нельзя, а снести целиком
    можно, если это решил тот, кто отвечает за учёт. Раньше эти два вопроса
    отвечались одной функцией, и из-за этого удаление было заперто вместе
    с правкой, а обход шёл через снятие одобрения.

    ⚠️ ОДНА ТРАНЗАКЦИЯ НА ВСЁ. Между проверкой статуса и удалением статус
    может смениться (замер 22.09.2026: проверка и удаление шли двумя
    отдельными запросами к пулу). Внутри транзакции строка отчёта берётся
    `FOR UPDATE` — параллельная смена статуса или вторая попытка удаления
    ждут, а не проскакивают мимо проверки.
    """
    p = await get_pool()
    async with p.acquire() as conn:
        async with conn.transaction():
            row = await _fetch_report_for_update(conn, id, user)
            if not row:
                raise HTTPException(status_code=404, detail="Not found")
            _ensure_can_delete(row["status"], user)
            await _ensure_no_live_export(conn, id, user["org_id"])
            # ⚠️ СНИМКИ ОБНОВЛЯЮТСЯ ДО УДАЛЕНИЯ, В ТОЙ ЖЕ ТРАНЗАКЦИИ. Запись
            # журнала получила их при отправке, но название могло измениться
            # позже; после удаления брать его будет неоткуда. Строки без
            # ссылки (осиротевшие раньше) условие не трогает.
            await conn.execute(
                "UPDATE odata_exports SET report_number=$1, report_title=$2"
                " WHERE report_id=$1 AND org_id=$3",
                id,
                row["title"],
                user["org_id"],
            )
            await conn.execute(
                "DELETE FROM reports WHERE id=$1 AND org_id=$2", id, user["org_id"]
            )
    return None


@router.post("/{id}/receipts")
async def add_receipts(id: int, r: ReceiptsIn, user: dict = Depends(get_current_user)):
    """Добавить чеки в отчёт. Отдельная ручка, а не PATCH всего состава: клиент
    (карточка чека) не знает актуальный состав, и две вкладки не затирают правки
    друг друга. Идемпотентна: чек, уже лежащий В ЭТОМ отчёте, повторно не
    вставляется и ошибкой не считается — двойной тап безопасен."""
    p = await get_pool()
    async with p.acquire() as conn:
        async with conn.transaction():
            rep = await _load_editable_report(conn, id, user)
            if r.receiptIds:
                # IDOR: те же правила, что при создании отчёта.
                owned = await conn.fetch(
                    "SELECT id, user_id FROM receipts "
                    "WHERE id = ANY($1::int[]) AND org_id = $2",
                    r.receiptIds,
                    user["org_id"],
                )
                if {row["id"] for row in owned} != set(r.receiptIds):
                    raise HTTPException(
                        status_code=403, detail="Один или несколько чеков недоступны"
                    )
                # Эталон здесь — автор ОТЧЁТА, а не тот, кто добавляет: состав
                # обязан остаться однородным даже если чеки кладёт админ.
                _ensure_same_owner(owned, rep["user_id"])
                # Где эти чеки уже лежат: в этом отчёте — пропускаем, в чужом —
                # 409 с понятным текстом (а не сырое нарушение констрейнта).
                existing = await conn.fetch(
                    "SELECT report_id, receipt_id FROM report_items "
                    "WHERE receipt_id = ANY($1::int[])",
                    r.receiptIds,
                )
                if any(e["report_id"] != id for e in existing):
                    raise HTTPException(status_code=409, detail=RECEIPT_TAKEN)
                here = {e["receipt_id"] for e in existing}
                for rid in (x for x in r.receiptIds if x not in here):
                    try:
                        await conn.execute(
                            "INSERT INTO report_items VALUES ($1,$2)", id, rid
                        )
                    except asyncpg.UniqueViolationError:
                        # Гонка: чек заняли между SELECT и INSERT.
                        raise HTTPException(status_code=409, detail=RECEIPT_TAKEN)
            rep = await _recalc_total(conn, id, user["org_id"])
            return await _with_receipt_ids(conn, rep)


@router.delete("/{id}/receipts/{receipt_id}")
async def remove_receipt(
    id: int, receipt_id: int, user: dict = Depends(get_current_user)
):
    """Убрать чек из отчёта. Сам чек НЕ удаляется — освобождается и снова
    доступен для другого отчёта. Идемпотентна: если чека в отчёте нет, ответ
    тот же (состав уже такой, какой просят). Возвращает обновлённый отчёт —
    у клиента сразу свежие receiptIds и total, без второго запроса."""
    p = await get_pool()
    async with p.acquire() as conn:
        async with conn.transaction():
            await _load_editable_report(conn, id, user)
            # org-scope и на связи: чужой чек не отвяжем даже теоретически.
            await conn.execute(
                "DELETE FROM report_items WHERE report_id=$1 AND receipt_id=$2 "
                "AND receipt_id IN (SELECT id FROM receipts WHERE org_id=$3)",
                id,
                receipt_id,
                user["org_id"],
            )
            rep = await _recalc_total(conn, id, user["org_id"])
            return await _with_receipt_ids(conn, rep)


@router.patch("/{id}")
async def update_status(
    id: int,
    s: StatusIn,
    background: BackgroundTasks,
    user: dict = Depends(get_current_user),
):
    # REP-ROLES: утверждение — только бухгалтер/админ. Отправку на проверку
    # и отзыв в черновик автор делает сам, поэтому гейт только на два статуса.
    if s.status in APPROVAL_STATUSES:
        _require_approver(user)
    причина = (s.reason or "").strip()
    if s.status == "Отклонён" and not причина:
        raise HTTPException(
            status_code=400,
            detail=(
                "Укажите причину отклонения — она уходит сотруднику "
                "в уведомлении и остаётся в отчёте"
            ),
        )
    p = await get_pool()
    # ⚠️ СНЯТИЕ ОДОБРЕНИЯ — ПРАВО УТВЕРЖДАЮЩЕГО, А НЕ АВТОРА (решение владельца
    # 04.09.2026). Гейт выше смотрит на КУДА переводят, а этот — на ОТКУДА:
    # «Черновик» в список утверждающих статусов не входит, поэтому автор своего
    # одобренного отчёта возвращал его в работу сам и снова менял состав. Отчёт,
    # принятый к учёту, перестаёт быть личным делом автора.
    текущий = await _fetch_report(p, id, user)
    if not текущий:
        raise HTTPException(status_code=404, detail="Not found")
    # ⚠️ ПОВТОРНОЕ НАЖАТИЕ НЕ ПЛОДИТ СОБЫТИЙ (T159, блок А). Статус
    # переписываем молча, событие и письмо — НЕ шлём. Решение владельца
    # 10.09.2026, вариант ⓐ, и довод против 409: «человек нажал, ответ
    # не дошёл, нажал снова — и получил ошибку на ВЕРНОМ действии». Это
    # «отказ на исправном» из наших же наблюдений; при плохой сети повтор
    # обязан быть безобидным.
    # ⚠️ ПОЧЕМУ НЕ РАННИЙ RETURN ДО UPDATE: ответ ручки обязан остаться
    # одинаковым при первом и втором нажатии — фронт кладёт его в список
    # (T7). Ранний выход отдал бы другую форму, и карточка разъехалась бы
    # с базой на пустом месте.
    повтор = текущий["status"] == s.status
    if текущий["status"] == "Одобрен" and not can_see_all(user["role"]):
        raise HTTPException(
            status_code=403,
            detail=(
                "Снять одобрение может только бухгалтер или администратор — "
                "обратитесь к тому, кто отчёт одобрил"
            ),
        )
    # REP-ACL: сотрудник меняет статус только своего отчёта; чужой — 404.
    if can_see_all(user["role"]):
        row = await p.fetchrow(
            "UPDATE reports SET status=$1 WHERE id=$2 AND org_id=$3 RETURNING *",
            s.status,
            id,
            user["org_id"],
        )
    else:
        row = await p.fetchrow(
            "UPDATE reports SET status=$1 WHERE id=$2 AND org_id=$3 AND user_id=$4 "
            "RETURNING *",
            s.status,
            id,
            user["org_id"],
            user["id"],
        )
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    # ⚠️ ПРИЧИНА ПИШЕТСЯ ОТДЕЛЬНЫМ ЗАПРОСОМ, И ЭТО НЕ ЛЕНЬ, А ЦЕНА УРОКА.
    # Первая редакция дописывала `reject_reason` в тот же UPDATE — текст
    # запроса менялся, зеркала двойника переставали его узнавать, и сторож
    # `test_mirror_gaps` тут же показал последствие: статус ЧУЖОГО отчёта
    # начал меняться без проверки автора, 404 стал 200. Пока `test_api.py`
    # не переведён на живую базу (T36), смена текста запроса в роутере
    # означает починку двойника вместо работы. Доступ проверен запросом
    # выше — этот идёт по уже возвращённой строке.
    свежая = await p.fetchrow(
        "UPDATE reports SET reject_reason=$1 WHERE id=$2 RETURNING *",
        причина or None,
        row["id"],
    )
    row = свежая or row
    # Ответ ТОЙ ЖЕ формы, что у GET /api/reports/ — вместе с receiptIds.
    # Иначе клиент, подставляя ответ PATCH в список, терял состав отчёта
    # (карточка показывала «0 чеков»). Разная форма ответов на один ресурс —
    # источник таких багов, поэтому добираем состав здесь, а не мержим на фронте.
    items = await p.fetch(
        "SELECT receipt_id FROM report_items WHERE report_id=$1 ORDER BY receipt_id",
        id,
    )
    d = dict(row)
    d["receiptIds"] = [i["receipt_id"] for i in items]
    d["in_1c"] = await _в_1с(p, user["org_id"], id)
    if not повтор:
        await _сказать_о_статусе(p, d, user, причина, background)
    return d


async def _сказать_о_статусе(p, отчёт: dict, кто: dict, причина: str, background):
    """Событие в колокольчик и письмо — ОДНО событие, два канала (T159).

    ⚠️ ПОРЯДОК ВАЖЕН: сначала строка события в базе, потом письмо фоном.
    Строка — наша, и она обязана лечь; письмо уходит через чужой SMTP,
    и его недоступность не может отменить уже случившийся факт.

    ⚠️ СЕБЕ НЕ ПИШЕМ. Бухгалтер, отклонивший отчёт, уже знает, что отклонил;
    автор, отправивший отчёт на проверку, знает, что отправил. Правило
    владельца: уведомление, которое человек может предсказать сам,
    обесценивает остальные.
    """
    статус = отчёт["status"]
    название = отчёт.get("title") or f"№{отчёт['id']}"
    ссылка = f"{APP_URL}/otchety" if APP_URL else ""

    if статус in ("Одобрен", "Отклонён"):
        автор = отчёт.get("user_id")
        if not автор or автор == кто["id"]:
            return
        вид = ОТЧЁТ_ОДОБРЕН if статус == "Одобрен" else ОТЧЁТ_ОТКЛОНЁН
        заголовок = f"Отчёт {статус.lower()}: {название}"
        await записать_событие(
            p,
            адресаты=[автор],
            org_id=отчёт["org_id"],
            вид=вид,
            заголовок=заголовок,
            текст=причина or None,
            report_id=отчёт["id"],
        )
        for адрес in await почты_адресатов(p, [автор]):
            background.add_task(
                send_report_status_email, адрес, название, статус, причина, ссылка
            )
        return

    if статус == "На проверке":
        адресаты = await кому_управляющим(p, отчёт["org_id"], кроме=кто["id"])
        if not адресаты:
            return
        # ⚠️ ИМЯ АВТОРА БЕРЁМ ИЗ БАЗЫ, А НЕ У НАЖАВШЕГО КНОПКУ. Обычно это
        # один человек, но админ может отправить на проверку чужой отчёт —
        # и тогда управляющий прочёл бы «Админ отправил отчёт», хотя отчёт
        # сотрудника. Событие говорит, ЧЕЙ отчёт, а не кто нажал.
        # Нашёл тест живого контура: в подменённой авторизации имя смотрящего
        # отличается от имени автора, и подмена сразу это показала.
        строка_автора = await p.fetchrow(
            "SELECT first_name, last_name FROM users WHERE id=$1",
            отчёт.get("user_id"),
        )
        автор_имя = (
            " ".join(
                х
                for х in (
                    (строка_автора or {}).get("first_name"),
                    (строка_автора or {}).get("last_name"),
                )
                if х
            )
            or "Сотрудник"
        )
        сумма = f"{отчёт.get('total') or 0:.2f} ₽"
        await записать_событие(
            p,
            адресаты=адресаты,
            org_id=отчёт["org_id"],
            вид=ОТЧЁТ_НА_ПРОВЕРКУ,
            заголовок=f"Отчёт на проверку: {название}",
            текст=f"{автор_имя} · {сумма}",
            report_id=отчёт["id"],
        )
        for адрес in await почты_адресатов(p, адресаты):
            background.add_task(
                send_report_submitted_email, адрес, название, автор_имя, сумма, ссылка
            )
