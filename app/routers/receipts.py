import base64
import binascii
import json
import logging
from datetime import date
from typing import List, Optional

import asyncpg
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse, Response
from pydantic import BaseModel

from app.auth import can_delete_any, can_see_all, get_current_user
from app.categorization import DEFAULT_FALLBACK, categorize
from app.database import get_pool
from app.sql_builder import собрать_set
from app.parsers.fns_parser import parse_fns_response
from app.routers.fns import спросить_фнс
from app.parsers.items_parser import parse_fns_items, parse_ocr_items
from app.parsers.ocr_parser import parse_ocr_response
from app.storage import s3

# Срок жизни подписанной ссылки на фото. Пять минут — столько нужно браузеру,
# чтобы открыть картинку. Длинный срок превращает приватный бакет в публичный
# для всякого, кому ссылка попалась на глаза (журналы, история браузера).
PHOTO_URL_TTL_SECONDS = 300

logger = logging.getLogger(__name__)

# Что PATCH /receipts/{id} вправе менять. Имя колонки в SQL берётся ТОЛЬКО
# отсюда: пользовательская строка идентификатором не станет (T38).
ПОЛЯ_ЧЕКА = ("payment", "org", "category_id", "category_manual")

router = APIRouter(prefix="/api/receipts", tags=["receipts"])

# Enumerable values for `source` — the channel a receipt arrived via.
#   manual    — user typed it in
#   qr_scan   — scanned a fiscal QR; FNS lookup succeeded
#   photo_ocr — OCR'd a photo via Claude Vision
#   fns       — created from FNS data through some other flow (reserved)
DEFAULT_SOURCE = "manual"


class ReceiptIn(BaseModel):
    date: date
    org: str
    category: Optional[str] = None
    payment: Optional[str] = None
    amount: float
    employee: Optional[str] = None
    fn: Optional[str] = None  # deprecated — фронт пока шлёт сюда; переходный alias
    kkt_fn: Optional[str] = None  # фискальный номер ККТ (заменяет fn)
    # ⚠️ ФД И ФПД ПРИНИМАЮТСЯ ОТ КЛИЕНТА С 31.08.2026 (T132, шаг ⓪). До этого
    # они брались ТОЛЬКО из `raw_data`, то есть только когда ФНС ответила, —
    # а нужны они ровно в обратном случае. Замер 31.08.2026: строка QR нигде
    # не хранится, `ReceiptIn` не принимал ни `fd_num`, ни `fpd`, и собрать
    # запрос к ФНС по сохранённому чеку было НЕЧЕМ. Три поля вместо строки —
    # решение владельца: строка «готова к отправке», три поля — нет, и они
    # всё равно нужны для дедупликации и сверки с бумагой.
    fd_num: Optional[str] = None  # ФД — номер фискального документа
    fpd: Optional[str] = None  # ФПД — фискальный признак документа
    raw_data: Optional[dict] = None
    source: Optional[str] = None  # 'manual' | 'qr_scan' | 'photo_ocr' | 'fns'
    photo_url: Optional[str] = None  # external URL (Cloudflare R2 etc.) when set
    photo_key: Optional[str] = None  # ключ объекта в приватном бакете (№3)


@router.get("/")
async def get_receipts(user: dict = Depends(get_current_user)):
    p = await get_pool()
    # A-ACL: accountant/admin видят все чеки орг; employee — только свои (по автору).
    # Карточке чека нужно не только «занят ли», но и КУДА идти: чек лежит ровно
    # в одном отчёте (uq_report_items_receipt_id), отцепить его из карточки
    # нельзя — без имени отчёта пользователь в тупике.
    # Тот же уникальный индекс делает LEFT JOIN связью 1:0..1, поэтому строки не
    # размножаются, а in_report считается из самого джойна — отдельный EXISTS
    # не нужен. `receipts.*` вместо `*`: при джойне звёздочка вытянула бы ещё и
    # колонки reports/report_items (id, org_id, user_id… — конфликт имён).
    if can_see_all(user["role"]):
        rows = await p.fetch(
            """SELECT receipts.*,
                      (ri.receipt_id IS NOT NULL) AS in_report,
                      rep.id    AS report_id,
                      rep.title AS report_title
               FROM receipts
               LEFT JOIN report_items ri ON ri.receipt_id = receipts.id
               LEFT JOIN reports rep     ON rep.id = ri.report_id
               WHERE receipts.org_id=$1 ORDER BY receipts.date DESC""",
            user["org_id"],
        )
    else:
        rows = await p.fetch(
            """SELECT receipts.*,
                      (ri.receipt_id IS NOT NULL) AS in_report,
                      rep.id    AS report_id,
                      rep.title AS report_title
               FROM receipts
               LEFT JOIN report_items ri ON ri.receipt_id = receipts.id
               LEFT JOIN reports rep     ON rep.id = ri.report_id
               WHERE receipts.org_id=$1 AND receipts.user_id=$2
               ORDER BY receipts.date DESC""",
            user["org_id"],
            user["id"],
        )
    # ⚠️ ПРИЗНАК СЧИТАЕТСЯ ЗДЕСЬ, А НЕ ВО ФРОНТЕ (T132, шаг ①). Правило
    # «есть ФН+ФД+ФПД и пусто raw_data» живёт в ОДНОМ месте: две копии условия
    # разошлись бы при первой же правке, и кнопка появлялась бы там, где
    # дозапрос невозможен, — либо не появлялась там, где возможен.
    return [с_признаком_дозапроса(r) for r in rows]


@router.get("/suggest-payment")
async def suggest_payment(org: str, user: dict = Depends(get_current_user)):
    p = await get_pool()
    row = await p.fetchrow(
        """
        SELECT payment FROM receipts
        WHERE org=$1 AND org_id=$2 AND payment IS NOT NULL AND payment <> 'Не указано'
        GROUP BY payment ORDER BY COUNT(*) DESC LIMIT 1
    """,
        org,
        user["org_id"],
    )
    return {"payment": row["payment"] if row else None}


@router.get("/{id}/photo")
async def get_receipt_photo(id: int, user: dict = Depends(get_current_user)):
    """Фото чека. ТРИ ИСТОЧНИКА, ПОРЯДОК ЗНАЧИМ И ЗАКРЕПЛЁН ТЕСТАМИ.

    photo_key (приватный бакет) → photo_url (внешний адрес) → base64 внутри
    raw_data. Порядок именно такой, потому что до конца S-40 старые и новые
    чеки живут ОДНОВРЕМЕННО: у старых снимок в базе, у новых — в хранилище.
    Это стык, на котором ломается тихо: перевернётся порядок — часть чеков
    начнёт отдавать устаревший источник, и снаружи это выглядит как «фото
    просто другое», а не как ошибка.
    """
    p = await get_pool()
    # A-ACL: employee видит фото только своего чека; accountant/admin — любого в орг.
    if can_see_all(user["role"]):
        row = await p.fetchrow(
            "SELECT photo_key, photo_url, raw_data FROM receipts WHERE id=$1 AND org_id=$2",
            id,
            user["org_id"],
        )
    else:
        row = await p.fetchrow(
            "SELECT photo_key, photo_url, raw_data FROM receipts WHERE id=$1 AND org_id=$2 AND user_id=$3",
            id,
            user["org_id"],
            user["id"],
        )
    if not row:
        raise HTTPException(status_code=404, detail="Receipt not found")
    if row["photo_key"]:
        cfg = s3.S3Config.from_env()
        if cfg is None:
            # Ключ есть, а хранилище не настроено — это расхождение настроек,
            # а не отсутствие фото. Молчать нельзя: 404 читался бы как «снимка нет».
            raise HTTPException(status_code=503, detail="Storage is not configured")
        try:
            content, content_type = await s3.get_object(cfg, row["photo_key"])
        except s3.ObjectNotFound:
            # Ключ в базе есть, объекта в бакете нет — рассинхрон, но для
            # пользователя это именно «фото нет».
            logger.warning("Фото по ключу отсутствует в хранилище, чек id=%s", id)
            raise HTTPException(status_code=404, detail="No photo for this receipt")
        except s3.StorageError:
            # 152-ФЗ: наружу не отдаём ни ключ, ни адрес, ни ответ поставщика.
            logger.warning("Хранилище не отдало фото, чек id=%s", id)
            raise HTTPException(status_code=502, detail="Storage is unavailable")
        # no-store: фото — персональные данные, в кэше промежуточных узлов
        # им не место.
        return Response(
            content=content,
            media_type=content_type,
            headers={"Cache-Control": "no-store"},
        )
    if row["photo_url"]:
        return RedirectResponse(url=row["photo_url"], status_code=302)
    raw = row["raw_data"] if isinstance(row["raw_data"], dict) else {}
    photo_b64 = raw.get("photo_base64") if raw else None
    if not photo_b64:
        raise HTTPException(status_code=404, detail="No photo for this receipt")
    try:
        photo_bytes = base64.b64decode(photo_b64, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=500, detail="Corrupt photo data")
    return Response(content=photo_bytes, media_type="image/jpeg")


async def _fetch_receipt(conn, id: int, user: dict):
    """Один чек в КАНОНИЧЕСКОЙ форме — той же, что отдаёт список: колонки чека
    + in_report / report_id / report_title.

    Одна точка на GET деталей и на оба выхода PATCH: если какой-то ответ вернёт
    «урезанный» чек, клиент подставит его в список и потеряет поля — ровно так
    в отчётах появлялась карточка с «0 чеков». A-ACL: employee — только свой.
    """
    if can_see_all(user["role"]):
        return await conn.fetchrow(
            """SELECT receipts.*,
                      (ri.receipt_id IS NOT NULL) AS in_report,
                      rep.id    AS report_id,
                      rep.title AS report_title
               FROM receipts
               LEFT JOIN report_items ri ON ri.receipt_id = receipts.id
               LEFT JOIN reports rep     ON rep.id = ri.report_id
               WHERE receipts.id=$1 AND receipts.org_id=$2""",
            id,
            user["org_id"],
        )
    return await conn.fetchrow(
        """SELECT receipts.*,
                  (ri.receipt_id IS NOT NULL) AS in_report,
                  rep.id    AS report_id,
                  rep.title AS report_title
           FROM receipts
           LEFT JOIN report_items ri ON ri.receipt_id = receipts.id
           LEFT JOIN reports rep     ON rep.id = ri.report_id
           WHERE receipts.id=$1 AND receipts.org_id=$2
             AND receipts.user_id=$3""",
        id,
        user["org_id"],
        user["id"],
    )


@router.get("/{id}")
async def get_receipt(id: int, user: dict = Depends(get_current_user)):
    p = await get_pool()
    # A-ACL: employee видит только свой чек; accountant/admin — любой в орг.
    # Карточку открывают и напрямую — форма чека одинакова независимо от пути.
    row = await _fetch_receipt(p, id, user)
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    return с_признаком_дозапроса(row)


def _similar_receipt_brief(row) -> dict:
    """Краткая карточка похожего чека для body.warning (задача №9 фаза A): фронт
    рисует inline-баннер без второго GET. amount: Decimal → float (иначе JSON
    падает); date: datetime.date → ISO-строка. org может быть legacy-строкой."""
    return {
        "id": row["id"],
        "org": row["org"],
        "amount": float(row["amount"]) if row["amount"] is not None else None,
        "date": row["date"].isoformat() if row["date"] else None,
    }


async def resolve_category_id(db, org_id: int, category_name: str):
    """Имя статьи → category_id для конкретной орг (справочник per-org, Фикс №1).
    Если имени нет — фолбэк на «Прочие хозрасходы» той же орг. None, если и фолбэка
    нет (орг ещё не засеяна — напр. legacy/тестовые данные). db = pool или conn."""
    row = await db.fetchrow(
        "SELECT id FROM categories WHERE org_id=$1 AND name=$2 LIMIT 1",
        org_id,
        category_name,
    )
    if row:
        return row["id"]
    row = await db.fetchrow(
        "SELECT id FROM categories WHERE org_id=$1 AND name=$2 LIMIT 1",
        org_id,
        DEFAULT_FALLBACK,
    )
    return row["id"] if row else None


def _dup_item(row, *, is_new, in_report) -> dict:
    """Элемент warning.duplicates (задача №9 фаза C). deletable = нет надёжного
    фискального номера (kkt_fn IS NULL → photo_ocr/manual): фронт по нему ставит
    галочку «удалить» по умолчанию, у ФНС/qr_scan — снимает (юр. сила). amount:
    Decimal→float, date: date→ISO. in_report/is_new передаёт вызывающий."""
    return {
        "id": row["id"],
        "org": row["org"],
        "amount": float(row["amount"]) if row["amount"] is not None else None,
        "date": row["date"].isoformat() if row["date"] else None,
        "source": row["source"],
        "deletable": row["kkt_fn"] is None,
        "in_report": in_report,
        "is_new": is_new,
    }


_МЕСЯЦЫ = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)


def _сообщение_о_дубле(строки, совпало: str) -> str:
    """Текст предупреждения о дубле — С ДАТОЙ НАЙДЕННОГО ЧЕКА.

    ⚠️ ТРЕБОВАНИЕ ВЛАДЕЛЬЦА (№25, мера А): не «такой чек уже есть», а
    «этот чек уже добавлен 2 августа». Довод дословно: человек не помнит,
    сканировал он этот чек или нет, и предупреждение отвечает ровно на
    этот вопрос. С датой он РЕШАЕТ, без даты — гадает.

    ⚠️ ДАТА ЧЕКА, А НЕ ДАТА ДОБАВЛЕНИЯ. Человек ищет в памяти покупку,
    а не своё действие в приложении: «второго покупал» вспоминается,
    «второго заводил в приложение» — нет.

    Дат может быть несколько (окно ±3 дня): называем ВСЕ различные, иначе
    сообщение утверждает больше, чем знает. Порядок — по возрастанию,
    чтобы читалось как перечисление, а не как случайный набор.
    """
    даты = sorted({с["date"] for с in строки if с.get("date")})
    if not даты:
        # Дат нет вовсе — форма без даты, чтобы не утверждать лишнего.
        return f"Возможный дубль: {совпало} — как у чека за последние 7 дней"
    словами = ", ".join(f"{д.day} {_МЕСЯЦЫ[д.month - 1]}" for д in даты)
    начало = "Этот чек уже добавлен" if len(даты) == 1 else "Такие чеки уже добавлены"
    return f"{начало} {словами} — {совпало}"


@router.post("/")
async def create_receipt(r: ReceiptIn, user: dict = Depends(get_current_user)):
    p = await get_pool()
    source = r.source or DEFAULT_SOURCE
    effective_kkt_fn = r.kkt_fn or r.fn  # переходный fallback: фронт пока шлёт fn
    org_id = user["org_id"]

    # ── Парсинг raw_data ДО категоризации и дедупа: даёт org_brand (приоритет бренда,
    # Фикс A2) и effective_org_inn (composite-ветки). ФНС-формат для qr_scan/fns,
    # OCR-формат для photo_ocr; оба парсера возвращают ОДИН набор ключей (см. INSERT).
    # Сбой → {}, чек всё равно создастся. В лог НЕ пишем raw_data (ИНН/кассир, 152-ФЗ).
    parsed: dict = {}
    if r.raw_data:
        try:
            if source in ("qr_scan", "fns"):
                parsed = parse_fns_response(r.raw_data)
            elif source == "photo_ocr":
                parsed = parse_ocr_response(r.raw_data)
        except Exception as e:  # noqa: BLE001 — парсинг не должен блокировать чек
            logger.warning(
                "raw_data parse failed (source=%s): %s", source, type(e).__name__
            )
            parsed = {}
    effective_org_inn = parsed.get("org_inn")  # уже провалидирован в parse_*_response
    # ⚠️ ФД — из ответа ФНС, а если его нет, ИЗ КЛИЕНТА (T132, шаг ⓪). Документ
    # уникален парой (ФН, ФД), и до 31.08.2026 при молчащей ФНС ФД не было
    # вовсе — значит дедупликация по паре не срабатывала ровно тогда, когда
    # человек пересканирует чек, не дождавшись ответа. Владелец назвал это
    # вторым доводом за хранение трёх полей, и он верен.
    effective_fd_num = parsed.get("fd_num") or r.fd_num

    # Категория: если пользователь не задал (или «Не указано») — авто. Приоритет
    # (Фикс №4 + A2): позиции → бренд (org_brand) → юрлицо (org) → «Прочие хозрасходы».
    # Имя категории — промежуточное: резолвим в category_id per-org (канон), саму
    # строку category в колонку больше НЕ пишем (вариант B; DROP COLUMN — отдельный ЧП).
    category = r.category
    if not category or category == "Не указано":
        items = []
        if r.raw_data:
            if source in ("qr_scan", "fns"):
                items = parse_fns_items(r.raw_data)
            elif source == "photo_ocr":
                items = parse_ocr_items(r.raw_data)
        category = categorize(r.org or "", items, brand=parsed.get("org_brand"))
    category_id = await resolve_category_id(p, org_id, category)

    # Надёжный фискальный номер есть только у не-photo_ocr источников с номером.
    # photo_ocr пишет kkt_fn=NULL (Вариант A) — его OCR-номер не считается надёжным.
    has_reliable_fn = bool(effective_kkt_fn) and source != "photo_ocr"

    # Дедуп — 4 ветки в строгом порядке. Жёсткий 409 только в ветках 0 и 1
    # (точные совпадения); ветки 2/3 — мягкое предупреждение (чек создаётся),
    # «лучше лишний чек, чем потерянный» (диагностика 26.05, фиксы C1/C2/C3).

    # ── Ветка 0 — двойной тап (90 сек). Только для fn-less чеков: у источников
    # с надёжным fn повторный тап = тот же fn и ловится веткой 1. Защита от дублей,
    # пока фронт не показывает warning (задача №9).
    if not has_reliable_fn:
        dbl = await p.fetchrow(
            """SELECT id FROM receipts
               WHERE date = $1 AND amount = $2 AND org_id = $3 AND source = $4
                 AND kkt_fn IS NULL
                 AND created_at > NOW() - INTERVAL '90 seconds'
               LIMIT 1""",
            r.date,
            r.amount,
            org_id,
            source,
        )
        if dbl:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "double_tap_detected",
                    "message": "Похожий чек только что добавлен. Подождите 90 секунд или измените данные.",
                    "existing_id": dbl["id"],
                },
            )

    # ── Ветка 1 — точный дубль фискального документа по паре (ФН, ФД), per-org,
    # бессрочно. ФН (kkt_fn) уникален на накопитель/кассу — общий для ВСЕХ чеков
    # заведения; документ уникален именно ПАРОЙ (kkt_fn=ФН, fd_num=ФД). Жёсткий
    # 409 только когда есть И ФН, И ФД; без fd_num (None) — в ключ не входим и
    # падаем в мягкие ветки 2/3 (иначе все чеки одной кассы схлопывались бы).
    if has_reliable_fn and effective_fd_num:
        existing = await p.fetchrow(
            "SELECT id FROM receipts WHERE kkt_fn=$1 AND fd_num=$2 AND org_id=$3",
            effective_kkt_fn,
            effective_fd_num,
            org_id,
        )
        if existing:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "duplicate_kkt_fn",
                    "message": "Чек с таким фискальным номером уже зарегистрирован",
                    "existing_id": existing["id"],
                },
            )

    # ── Ветки 2/3 — мягкое предупреждение по composite, окно 7 дней. Срабатывает
    # ровно одна (взаимоисключающие): сильная (есть ИНН) ИЛИ слабая (нет ИНН).
    # fn-фильтр динамический ($N = has_reliable_fn): чек с надёжным fn ищет дубль
    # ТОЛЬКО среди fn-less чеков (иначе два РАЗНЫХ qr с разными fn ложно совпали бы
    # по дате+сумме+ИНН); fn-less чек ищет среди всех — так ловятся ОБА направления
    # реального бага id3↔id4 (photo_ocr↔qr_scan). category/payment в ключ НЕ входят
    # (C3): пользователь меняет их после создания, ключ бы рассинхронизировался.
    # Собираем ВСЕ совпадения в окне 7 дней (fetch, не fetchrow): фронт покажет
    # их группой с чекбоксами для удаления лишних (задача №9 фаза C). Запрос ДО
    # INSERT — только что созданный чек добавим в массив явно после вставки (под
    # динамическим fn-фильтром он бы не попал). SELECT тянет source/kkt_fn (для
    # deletable) + EXISTS(report_items) AS in_report. created_at ASC — хронология.
    # Срабатывает ровно одна ветка: сильная (есть ИНН) ИЛИ слабая (нет ИНН).
    # ⚠️ МЕРА А (№25): КЛЮЧ ОТВЯЗАН ОТ ТОЧНОГО СОВПАДЕНИЯ ДАТЫ.
    #
    # Было `date = $1`. Живой случай 12.08.2026: ОДИН чек сохранён ТРИЖДЫ
    # (id 71, 72, 73, все по 3500, все photo_ocr), приложение промолчало.
    # Пары разошлись так: 71 и 72 — по ДАТЕ (владелец правил её руками),
    # 72 и 73 — по ОРГАНИЗАЦИИ (ошибка распознавания). Точное равенство
    # даты не совпало ни разу, и ветка не сработала.
    #
    # ⚠️ КОРЕНЬ, НАЗВАННЫЙ ВЛАДЕЛЬЦЕМ: ключ опирается на РЕДАКТИРУЕМЫЕ
    # и РАСПОЗНАННЫЕ поля. Дату правят руками, организацию читает модель.
    # Окно ±3 дня снимает первую половину; вторая (организация) остаётся
    # и лечится не здесь — см. трекер, вариант Б, он НЕ начат намеренно.
    #
    # ПОЧЕМУ ТРИ ДНЯ, А НЕ СЕМЬ: `created_at > NOW() - 7 days` уже
    # ограничивает выборку недавно ЗАВЕДЁННЫМИ чеками. Три дня — про дату
    # САМОГО ЧЕКА, и это запас на ручную правку и на ошибку распознавания
    # дня, а не на «покупал раз в неделю». Шире окно — больше регулярных
    # покупок попадёт в предупреждение зря.
    #
    # ⚠️ ЭТО ПРЕДУПРЕЖДЕНИЕ, А НЕ ЗАПРЕТ. Жёсткий запрет остаётся только
    # там, где он и был: уникальный индекс (kkt_fn, fd_num). Ложное
    # срабатывание стоит одного касания «всё равно добавить»;
    # пропущенный дубль стоит завышенного расхода в отчёте.
    ОКНО_ДНЕЙ = 3

    dup_confidence = dup_message = None
    similar_rows = []
    if effective_org_inn:
        similar_rows = await p.fetch(
            """SELECT id, org, amount, date, source, kkt_fn,
                      EXISTS(SELECT 1 FROM report_items ri WHERE ri.receipt_id = receipts.id) AS in_report
               FROM receipts
               WHERE date BETWEEN $1::date - $6::int AND $1::date + $6::int
                 AND amount = $2 AND org_inn = $3 AND org_id = $4
                 AND (NOT $5::boolean OR kkt_fn IS NULL)
                 AND created_at > NOW() - INTERVAL '7 days'
               ORDER BY created_at ASC""",
            r.date,
            r.amount,
            effective_org_inn,
            org_id,
            has_reliable_fn,
            ОКНО_ДНЕЙ,
        )
        if similar_rows:
            dup_confidence = "high"
            dup_message = _сообщение_о_дубле(
                similar_rows, "совпадают сумма и ИНН поставщика"
            )
    else:
        similar_rows = await p.fetch(
            """SELECT id, org, amount, date, source, kkt_fn,
                      EXISTS(SELECT 1 FROM report_items ri WHERE ri.receipt_id = receipts.id) AS in_report
               FROM receipts
               WHERE date BETWEEN $1::date - $5::int AND $1::date + $5::int
                 AND amount = $2 AND org_id = $3
                 AND (NOT $4::boolean OR kkt_fn IS NULL)
                 AND created_at > NOW() - INTERVAL '7 days'
               ORDER BY created_at ASC""",
            r.date,
            r.amount,
            org_id,
            has_reliable_fn,
            ОКНО_ДНЕЙ,
        )
        if similar_rows:
            dup_confidence = "low"
            dup_message = _сообщение_о_дубле(similar_rows, "совпадает сумма")

    # Вариант A: photo_ocr НЕ пишет номер в kkt_fn (OCR-номер ненадёжен, остаётся
    # только в raw_data.fn). Надёжные источники пишут kkt_fn — каноническую колонку
    # (старая fn убрана; приём r.fn ещё роутится в effective_kkt_fn выше для совместимости).
    if source == "photo_ocr":
        kkt_fn_to_save = None
    else:
        kkt_fn_to_save = effective_kkt_fn

    # kkt_fn колонка пишется из dedup-значения (kkt_fn_to_save), НЕ из parsed —
    # чтобы хранимое значение совпадало с тем, по которому шёл дедуп (ЧП C).
    try:
        async with p.acquire() as conn:
            async with conn.transaction():
                # ⚠️ НОВЫЕ КОЛОНКИ ДОБАВЛЯЮТСЯ ПОСЛЕДНИМИ — `sum_vat_0`,
                # `sum_no_vat` после `photo_key`, и по той же причине, что
                # `photo_key` когда-то: иначе сдвигаются позиции ВСЕХ параметров
                # ниже, и зеркало FakePool приходится перенумеровывать целиком.
                # На этом уже горели (NDS-CLEANUP ②, 33 красных теста).
                #
                # ⚠️ И ПОЯСНЕНИЕ СТОИТ ЗДЕСЬ, А НЕ ВНУТРИ SQL, — ЭТО ВТОРАЯ ЦЕНА
                # ТОГО ЖЕ УРОКА. Прежде оно лежало внутри литерала строкой `--`.
                # Приложение работало: asyncpg получает SQL с переводами строк.
                # Ломался ПРИБОР `sql_check`: он нормализует SQL как
                # `" ".join(sql.split())` (mirror_gaps.py:59), переводы строк
                # схлопываются, и `--` съедает ОСТАТОК ЗАПРОСА — «синтаксическая
                # ошибка в конце». Отказ вылезал в другом файле и три недели был
                # не виден вовсе: шаг CI падал раньше, на init_db (см. T89).
                # Правило записано в CLAUDE.md: комментарий НАД конструкцией.
                row = await conn.fetchrow(
                    """INSERT INTO receipts (
                        org_id, date, org, payment, amount, employee,
                        kkt_fn, raw_data, source, photo_url,
                        datetime, currency, operation_type, org_legal, org_brand,
                        org_inn, payment_form, payment_detail, card_last4,
                        tax_system, address, vat_0, vat_total,
                        kkt_serial, kkt_rn, fd_num, fpd, cashier, category_id, user_id,
                        vat_breakdown, photo_key,
                        sum_vat_0, sum_no_vat
                    ) VALUES (
                        $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,
                        $12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22,
                        $23,$24,$25,$26,$27,$28,$29,$30,$31,$32,$33,$34
                    ) RETURNING *""",
                    user["org_id"],
                    r.date,
                    r.org,
                    r.payment,
                    r.amount,
                    r.employee,
                    kkt_fn_to_save,
                    r.raw_data,
                    source,
                    r.photo_url,
                    parsed.get("datetime"),
                    parsed.get("currency"),
                    parsed.get("operation_type"),
                    parsed.get("org_legal"),
                    parsed.get("org_brand"),
                    parsed.get("org_inn"),
                    parsed.get("payment_form"),
                    parsed.get("payment_detail"),
                    parsed.get("card_last4"),
                    parsed.get("tax_system"),
                    parsed.get("address"),
                    parsed.get("vat_0"),
                    parsed.get("vat_total"),
                    parsed.get("kkt_serial"),
                    parsed.get("kkt_rn"),
                    # ⚠️ ОТВЕТ ФНС В ПРИОРИТЕТЕ, КЛИЕНТ — ЗАПАСНОЙ ПУТЬ, а не
                    # наоборот: разобранный ответ достовернее того, что прочли
                    # с картинки или ввели руками. Клиентское значение доезжает
                    # ровно тогда, когда ФНС промолчала, — ради этого и заведено.
                    parsed.get("fd_num") or r.fd_num,
                    parsed.get("fpd") or r.fpd,
                    parsed.get("cashier"),
                    category_id,
                    user["id"],
                    parsed.get("vat_breakdown"),
                    r.photo_key,
                    parsed.get("sum_vat_0"),
                    parsed.get("sum_no_vat"),
                )
                # Позиции — best-effort: вложенная транзакция (SAVEPOINT), чтобы
                # сбой вставки/парсинга позиций откатывал ТОЛЬКО их, а чек оставался.
                # Выбор парсера по источнику (ФНС-коды vs OCR-строки, копейки vs рубли).
                if r.raw_data and source in ("qr_scan", "fns", "photo_ocr"):
                    parse_items = (
                        parse_fns_items
                        if source in ("qr_scan", "fns")
                        else parse_ocr_items
                    )
                    try:
                        async with conn.transaction():
                            for item in parse_items(r.raw_data):
                                await conn.execute(
                                    """INSERT INTO receipt_items
                                       (receipt_id, position, name, quantity, price, sum, vat_rate)
                                       VALUES ($1,$2,$3,$4,$5,$6,$7)""",
                                    row["id"],
                                    item["position"],
                                    item["name"],
                                    item["quantity"],
                                    item["price"],
                                    item["sum"],
                                    item["vat_rate"],
                                )
                    except Exception as e:  # noqa: BLE001 — позиции не валят чек
                        logger.warning(
                            "items insert failed for receipt %s (source=%s): %s",
                            row["id"],
                            source,
                            type(e).__name__,
                        )
    except asyncpg.exceptions.UniqueViolationError:
        # Дедуп выше — per-org (WHERE kkt_fn=$1 AND fd_num=$2 AND org_id=$3), а
        # partial-unique индекс receipts_kkt_fn_fd_unique — глобальный по паре
        # (kkt_fn, fd_num). Если один и тот же документ (ФН+ФД) пришёл в РАЗНЫЕ
        # org, SELECT-дедуп промахнётся, а индекс поймает на INSERT — 409 вместо 500.
        raise HTTPException(
            status_code=409,
            detail={
                "error": "duplicate_kkt_fn_cross_org",
                "message": "Чек с таким фискальным номером уже зарегистрирован в другой организации",
            },
        )
    # Ответ создания — в той же канонической форме, что элемент списка
    # (RETURNING * не знает про in_report/report_id/report_title). Иначе клиент,
    # добавив созданный чек в список, получит строку беднее соседних.
    result = с_признаком_дозапроса(await _fetch_receipt(p, row["id"], user) or row)
    if dup_confidence:
        # duplicates = существующие совпадения (created_at ASC) + только что
        # созданный чек (is_new=true, in_report=false: он ещё ни в одном отчёте).
        # similar_receipt[_id] — deprecated-поля для старого фронта (фаза A),
        # указывают на первый существующий дубль; фронт переходит на duplicates.
        duplicates = [
            _dup_item(s, is_new=False, in_report=bool(s["in_report"]))
            for s in similar_rows
        ]
        duplicates.append(_dup_item(row, is_new=True, in_report=False))
        result["warning"] = {
            "type": "possible_duplicate",
            "confidence": dup_confidence,
            "message": dup_message,
            "similar_receipt_id": similar_rows[0]["id"],  # deprecated
            "similar_receipt": _similar_receipt_brief(similar_rows[0]),  # deprecated
            "duplicates": duplicates,
        }
    return result


@router.post("/dedupe-cleanup/")
async def dedupe_cleanup(user: dict = Depends(get_current_user)):
    # A-ACL: орг-уровневая деструктивная чистка дублей — только admin.
    if not can_delete_any(user["role"]):
        raise HTTPException(status_code=403, detail="Только для администратора")
    p = await get_pool()
    rows = await p.fetch(
        """
        SELECT MIN(id) AS keep_id, date, amount, org, COUNT(*) AS cnt
        FROM receipts WHERE org_id=$1
        GROUP BY date, amount, org
        HAVING COUNT(*) > 1
    """,
        user["org_id"],
    )
    deleted_total = 0
    kept_total = 0
    for row in rows:
        await p.execute(
            "DELETE FROM receipts WHERE date=$1 AND amount=$2 AND org=$3 AND org_id=$4 AND id <> $5",
            row["date"],
            row["amount"],
            row["org"],
            user["org_id"],
            row["keep_id"],
        )
        deleted_total += row["cnt"] - 1
        kept_total += 1

    return {"deleted": deleted_total, "kept": kept_total}


class BulkDeleteIn(BaseModel):
    ids: List[int]
    force: bool = False  # пробивает ТОЛЬКО ФНС-защиту, НЕ in_report (Q2)


async def _drop_photo_objects(p, ids: list, user: dict) -> list:
    """Убрать снимки удаляемых чеков ИЗ ХРАНИЛИЩА. Возвращает id, где не вышло.

    ПОЧЕМУ ЭТО ДЕЛАЕТСЯ ДО УДАЛЕНИЯ СТРОКИ, А НЕ ПОСЛЕ. До задачи №3 снимок
    лежал внутри чека (`raw_data.photo_base64`) и исчезал вместе со строкой.
    Вынеся его в бакет, мы СОЗДАЛИ бы состояние «пользователь удалил, а данные
    целы»: объект остался бы в хранилище навсегда и невидимым ни в одном
    интерфейсе. Для 152-ФЗ это прямая проблема, а не неопрятность.

    Порядок «сначала объект, потом строка» выбран сознательно: если хранилище
    недоступно, чек НЕ удаляется и пользователь может повторить. Обратный
    порядок оставлял бы сироту молча — то есть ровно ту дыру, которую строка
    S-48 и закрывает. Отсутствие объекта в бакете считается успехом
    (`delete_object` идемпотентен): повтор удаления обязан заканчиваться тем же
    состоянием.
    """
    if not ids:
        return []
    if can_delete_any(user["role"]):
        rows = await p.fetch(
            "SELECT id, photo_key FROM receipts WHERE id = ANY($1::int[]) AND org_id = $2",
            ids,
            user["org_id"],
        )
    else:
        rows = await p.fetch(
            "SELECT id, photo_key FROM receipts WHERE id = ANY($1::int[]) AND org_id = $2 AND user_id = $3",
            ids,
            user["org_id"],
            user["id"],
        )
    with_photo = [(r["id"], r["photo_key"]) for r in rows if r["photo_key"]]
    if not with_photo:
        return []
    cfg = s3.S3Config.from_env()
    if cfg is None:
        # Ключи есть, а хранилище не настроено — удалять нечем. Молча
        # продолжить значило бы оставить ПД в бакете, о котором мы «не знаем».
        logger.warning("Удаление чеков: есть ключи фото, но хранилище не настроено")
        return [rid for rid, _ in with_photo]
    failed = []
    for rid, key in with_photo:
        try:
            await s3.delete_object(cfg, key)
        except s3.StorageError:
            logger.warning("Не удалось убрать фото из хранилища, чек id=%s", rid)
            failed.append(rid)
    return failed


@router.post("/bulk-delete")
async def bulk_delete_receipts(
    body: BulkDeleteIn, user: dict = Depends(get_current_user)
):
    """Массовое удаление дублей из баннера (задача №9 фаза C). Защиты:
    • in_report — блок ВСЕГДА (нельзя молча выкинуть чек из отчёта), force не пробивает;
    • надёжный фискальный номер (kkt_fn) — блок без force (у ФНС/qr_scan юр. сила);
    • чужие id молча отсеиваются (изоляция по org_id).
    Ответ всегда 200 с детализацией {deleted, blocked_fns, blocked_in_report}."""
    org_id = user["org_id"]
    deleted, blocked_fns, blocked_in_report = [], [], []
    if not body.ids:
        return {
            "deleted": deleted,
            "blocked_fns": blocked_fns,
            "blocked_in_report": blocked_in_report,
            "blocked_storage": [],
        }
    p = await get_pool()
    # Кандидаты — только чеки текущей орг; чужие id в выборку не попадают (изоляция).
    # A-ACL: admin может удалять любые в орг; employee/accountant — только свои (по
    # автору) — чужие молча отсеиваются из кандидатов, как и чужие org_id.
    if can_delete_any(user["role"]):
        rows = await p.fetch(
            """SELECT id, kkt_fn,
                      EXISTS(SELECT 1 FROM report_items ri WHERE ri.receipt_id = receipts.id) AS in_report
               FROM receipts WHERE id = ANY($1::int[]) AND org_id = $2""",
            body.ids,
            org_id,
        )
    else:
        rows = await p.fetch(
            """SELECT id, kkt_fn,
                      EXISTS(SELECT 1 FROM report_items ri WHERE ri.receipt_id = receipts.id) AS in_report
               FROM receipts WHERE id = ANY($1::int[]) AND org_id = $2 AND user_id = $3""",
            body.ids,
            org_id,
            user["id"],
        )
    for row in rows:
        if row["in_report"]:
            blocked_in_report.append(row["id"])  # блок ВСЕГДА
        elif row["kkt_fn"] is not None and not body.force:
            blocked_fns.append(row["id"])  # ФНС-защита, пробивается force
        else:
            deleted.append(row["id"])
    # Снимки убираем ДО строк: не удалили фото — не удаляем и чек, иначе
    # в бакете останутся ПД без единой ссылки на них (S-48).
    blocked_storage = await _drop_photo_objects(p, deleted, user)
    if blocked_storage:
        deleted = [rid for rid in deleted if rid not in blocked_storage]
    if deleted:
        async with p.acquire() as conn:
            async with conn.transaction():
                # org-безопасная чистка связей (закрывает дыру изоляции 1.2):
                # report_items трогаем ТОЛЬКО для чеков своей орг.
                await conn.execute(
                    """DELETE FROM report_items WHERE receipt_id = ANY($1::int[])
                       AND receipt_id IN (SELECT id FROM receipts WHERE org_id = $2)""",
                    deleted,
                    org_id,
                )
                await conn.execute(
                    "DELETE FROM receipts WHERE id = ANY($1::int[]) AND org_id = $2",
                    deleted,
                    org_id,
                )
    return {
        "deleted": deleted,
        "blocked_fns": blocked_fns,
        "blocked_in_report": blocked_in_report,
        # Новая полоса ответа: чек не удалён, потому что не удалось убрать его
        # снимок из хранилища. Молчать нельзя — «удалено» без фото в бакете
        # и «не удалено вовсе» для пользователя разные вещи.
        "blocked_storage": blocked_storage,
    }


class ReceiptPatch(BaseModel):
    category: Optional[str] = None
    payment: Optional[str] = None
    org: Optional[str] = None


@router.patch("/{id}")
async def patch_receipt(
    id: int, r: ReceiptPatch, user: dict = Depends(get_current_user)
):
    p = await get_pool()
    org_id = user["org_id"]
    поля: dict = {}
    for k, v in [("payment", r.payment), ("org", r.org)]:
        if v is not None:
            поля[k] = v
    # Ручная смена категории: строку category в колонку НЕ пишем (вариант B), только
    # резолвим category_id server-side (per-org, НЕ из тела — IDOR-защита) и ставим
    # category_manual=TRUE — будущий батч-пересчёт (Фикс №4) такой чек не тронет.
    # Триггер — любой непустой r.category в патче (явный выбор имени фронтом).
    if r.category:
        поля["category_id"] = await resolve_category_id(p, org_id, r.category)
        поля["category_manual"] = True
    if not поля:
        # Нечего менять — отдаём чек в той же канонической форме, что и GET.
        row = await _fetch_receipt(p, id, user)
        if not row:
            raise HTTPException(status_code=404, detail="Not found")
        return с_признаком_дозапроса(row)
    sets, values = собрать_set(поля, ПОЛЯ_ЧЕКА)
    values.append(id)
    values.append(user["org_id"])
    where = f"WHERE id=${len(values) - 1} AND org_id=${len(values)}"
    # A-ACL: employee правит только свой чек; accountant/admin — любой в орг.
    if not can_see_all(user["role"]):
        values.append(user["id"])
        where += f" AND user_id=${len(values)}"
    row = await p.fetchrow(
        f"UPDATE receipts SET {sets} {where} RETURNING *",
        *values,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    # RETURNING * даёт только колонки таблицы. Перечитываем чек канонической
    # формой, иначе ответ PATCH окажется беднее элемента списка, и клиент,
    # подставив его, потеряет in_report / report_title.
    return с_признаком_дозапроса(await _fetch_receipt(p, id, user) or row)


@router.delete("/{id}")
async def delete_receipt(id: int, user: dict = Depends(get_current_user)):
    p = await get_pool()
    org_id = user["org_id"]
    # Снимок убираем ДО строки. Не вышло — чек НЕ удаляем и говорим об этом:
    # иначе в бакете останутся персональные данные, на которые больше ничто
    # не ссылается (S-48). Пользователь может повторить.
    if await _drop_photo_objects(p, [id], user):
        raise HTTPException(status_code=503, detail="Storage is unavailable")
    async with p.acquire() as conn:
        async with conn.transaction():
            # org-безопасная чистка связей (защита от IDOR P1, аналог bulk-delete):
            # report_items трогаем ТОЛЬКО для чеков своей орг.
            # A-ACL: admin удаляет любой чек орг; employee/accountant — только свой (по автору).
            if can_delete_any(user["role"]):
                await conn.execute(
                    """DELETE FROM report_items
                       WHERE receipt_id=$1
                       AND receipt_id IN (SELECT id FROM receipts WHERE org_id=$2)""",
                    id,
                    org_id,
                )
                await conn.execute(
                    "DELETE FROM receipts WHERE id=$1 AND org_id=$2", id, org_id
                )
            else:
                await conn.execute(
                    """DELETE FROM report_items
                       WHERE receipt_id=$1
                       AND receipt_id IN (SELECT id FROM receipts WHERE org_id=$2 AND user_id=$3)""",
                    id,
                    org_id,
                    user["id"],
                )
                await conn.execute(
                    "DELETE FROM receipts WHERE id=$1 AND org_id=$2 AND user_id=$3",
                    id,
                    org_id,
                    user["id"],
                )
    # Всегда 200 {"ok": True} — не палим существование чужих чеков (anti-enumeration).
    return {"ok": True}

# ─── T132: ДОЗАПРОС В ФНС ПО СОХРАНЁННОМУ ЧЕКУ ───────────────────────────
#
# ⚠️ ЗАЧЕМ. 31.08.2026 налоговая перестала отдавать данные: код 5 «нет
# информации» приходил на ЗАРЕГИСТРИРОВАННЫЕ чеки — владелец проверил два
# из них вручную на сайте, они там есть. Чек в такой день сохраняется по
# фото, с пустыми позициями и НДС. **До этой правки они оставались пустыми
# НАВСЕГДА**: перезапроса не было ни одного, а строки QR, которой он
# делается, нигде не хранилось.
#
# ⚠️ ДЁРГАЕТ ЧЕЛОВЕК КНОПКОЙ, НЕ ФОН — решение владельца. Планировщика в
# проекте нет вовсе, заводить его ради этого — работа другого класса
# (заведена строкой отдельно). Кнопка закрывает случай: человек видит
# неполный чек и жмёт, когда вспомнил.
#
# ⚠️ СТАРЫЕ ЧЕКИ НЕ ТРОГАЕМ — тоже решение владельца, дословно: «со старыми
# чеками не разбираемся вовсе». У них нет ФД и ФПД, дозапросить их нечем,
# и никаких пометок «невозможно» им не ставится.


def _можно_дозапросить(row) -> bool:
    """Есть ли чем повторить запрос в ФНС и нужно ли повторять.

    ⚠️ ПРИЗНАК ВЫВОДИТСЯ, А НЕ ХРАНИТСЯ — колонки под него нет и не нужно.
    Три поля (ФН, ФД, ФПД) появляются ТОЛЬКО из разобранного QR, а `raw_data`
    остаётся пустым ТОЛЬКО когда ФНС промолчала: при удачном ответе туда
    ложится её тело, при распознавании фото — тело OCR. Значит сочетание
    «три поля есть, raw_data пусто» и означает ровно «QR прочитан, ФНС не
    ответила». Старые чеки под условие не попадают сами: у них нет ФПД.
    """
    if not (row.get("kkt_fn") and row.get("fd_num") and row.get("fpd")):
        return False
    if not (row.get("datetime") and row.get("amount") is not None):
        return False
    return not row.get("raw_data")


def с_признаком_дозапроса(row) -> dict:
    """Одна форма чека на все ручки: список, карточка, правка, дозапрос.

    ⚠️ ЗАВЕДЕНО ПО КРАСНОМУ СТОРОЖУ, А НЕ ЗАРАНЕЕ. Признак добавили сперва
    только в список — и `test_receipt_shape_same_in_list_and_detail` покраснел
    сразу: форма чека в списке и в карточке обязана совпадать, иначе фронт
    видит кнопку в одном месте и не видит в другом.
    """
    д = dict(row)
    д["можно_дозапросить"] = _можно_дозапросить(д)
    return д


def _строка_qr(row) -> str:
    """Собирает строку QR из сохранённых полей — ту же, что читается с чека.

    Формат ФНС: t=ГГГГММДДTЧЧММ & s=рубли.копейки & fn=ФН & i=ФД & fp=ФПД
    & n=тип операции. Время до минуты — по нему налоговая и сверяет.
    """
    д = row["datetime"]
    n = _ТИП_ОПЕРАЦИИ.get(row.get("operation_type") or "purchase", 1)
    return (
        f"t={д.strftime('%Y%m%dT%H%M')}"
        f"&s={float(row['amount']):.2f}"
        f"&fn={row['kkt_fn']}&i={row['fd_num']}&fp={row['fpd']}&n={n}"
    )


_ТИП_ОПЕРАЦИИ = {
    "purchase": 1,
    "refund": 2,
    "expense": 3,
    "expense_refund": 4,
}

# Колонки, которые дозапрос вправе заполнить. ⚠️ ТОЛЬКО ПУСТЫЕ: человек мог
# ввести название и сумму руками, пока ждал налоговую, и затирать его работу
# ответом сервиса нельзя — он пришёл позже, но это не делает его главнее.
_ДОЗАПОЛНЯЕМЫЕ = (
    "org",
    "org_legal",
    "org_brand",
    "org_inn",
    "address",
    "tax_system",
    "payment_form",
    "kkt_serial",
    "kkt_rn",
    "cashier",
    "vat_0",
    "vat_total",
    "sum_vat_0",
    "sum_no_vat",
)


@router.post("/{id}/refetch-fns")
async def refetch_fns(id: int, user: dict = Depends(get_current_user)):
    """Повторяет запрос в ФНС по сохранённому чеку и дозаполняет пустое."""
    p = await get_pool()
    # Та же видимость, что у остальных ручек: employee — только свой чек.
    if can_see_all(user["role"]):
        row = await p.fetchrow(
            "SELECT * FROM receipts WHERE id=$1 AND org_id=$2", id, user["org_id"]
        )
    else:
        row = await p.fetchrow(
            "SELECT * FROM receipts WHERE id=$1 AND org_id=$2 AND user_id=$3",
            id,
            user["org_id"],
            user["id"],
        )
    if not row:
        raise HTTPException(status_code=404, detail="Not found")

    строка = dict(row)
    if not _можно_дозапросить(строка):
        # ⚠️ 409, А НЕ 400: запрос правильный, состояние чека не позволяет.
        # Текст называет причину и следующий шаг, а не «нельзя».
        raise HTTPException(
            status_code=409,
            detail=(
                "По этому чеку дозапрос невозможен: не сохранены ФД и ФПД "
                "(так заводились чеки до 31.08.2026) либо данные ФНС уже получены"
            ),
        )

    код, тело = await спросить_фнс(_строка_qr(строка))
    if код != 200:
        raise HTTPException(status_code=код, detail=тело.get("message", "Отказ ФНС"))

    разобранное = parse_fns_response(тело.get("raw") or {})
    правки = {
        к: разобранное.get(к)
        for к in _ДОЗАПОЛНЯЕМЫЕ
        if разобранное.get(к) is not None and not строка.get(к)
    }
    # `raw_data` кладём всегда: именно он снимает признак «можно дозапросить»,
    # иначе кнопка осталась бы на экране после удачного дозапроса.
    правки["raw_data"] = json.dumps(тело.get("raw") or {})

    sets, values = собрать_set(правки, (*_ДОЗАПОЛНЯЕМЫЕ, "raw_data"))
    обновлённый = await p.fetchrow(
        f"UPDATE receipts SET {sets} WHERE id = ${len(values) + 1} "
        f"AND org_id = ${len(values) + 2} RETURNING *",
        *values,
        id,
        user["org_id"],
    )
    return с_признаком_дозапроса(обновлённый)
