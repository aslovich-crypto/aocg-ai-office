from typing import List, Literal

import asyncpg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.auth import can_see_all, get_current_user
from app.database import get_pool

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
    # «Одобрен»: следующего шага пока нет — снятие одобрения появится
    # отдельной задачей REP-UNAPPROVE (право бухгалтера/админа).
    raise HTTPException(
        status_code=409,
        detail="Одобренный отчёт изменить нельзя: он принят к учёту",
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


async def _with_receipt_ids(conn, rep) -> dict:
    """Отчёт + receiptIds — форма элемента GET-списка (и ответа PATCH/POST)."""
    items = await conn.fetch(
        "SELECT receipt_id FROM report_items WHERE report_id=$1 ORDER BY receipt_id",
        rep["id"],
    )
    d = dict(rep)
    d["receiptIds"] = [i["receipt_id"] for i in items]
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
    result = []
    for rep in reports:
        d = dict(rep)
        d["receiptIds"] = [
            i["receipt_id"] for i in items if i["report_id"] == rep["id"]
        ]
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
    d["receipts"] = [dict(r) for r in receipts]
    return d


@router.delete("/{id}", status_code=204)
async def delete_report(id: int, user: dict = Depends(get_current_user)):
    """Удалить отчёт. Состав (report_items) уходит каскадом — ON DELETE CASCADE
    на report_id. Сами чеки НЕ трогаем: они просто освобождаются и снова
    доступны для другого отчёта."""
    p = await get_pool()
    row = await _fetch_report(p, id, user)
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    _ensure_editable(row["status"])
    await p.execute("DELETE FROM reports WHERE id=$1 AND org_id=$2", id, user["org_id"])
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
async def update_status(id: int, s: StatusIn, user: dict = Depends(get_current_user)):
    # REP-ROLES: утверждение — только бухгалтер/админ. Отправку на проверку
    # и отзыв в черновик автор делает сам, поэтому гейт только на два статуса.
    if s.status in APPROVAL_STATUSES:
        _require_approver(user)
    p = await get_pool()
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
    return d
