"""Выгрузка отчёта файлом — тонкая ручка над `app/exports/report_xlsx`.

⚠️ ОТДЕЛЬНЫМ ФАЙЛОМ, А НЕ В `reports.py`, И ЭТО НЕ ВКУСОВЩИНА: в `reports.py`
599 строк при сигнале ~300 из правил репозитория. Дописывать в него ещё один
домен — значит растить файл, который и так вдвое выше порога.

⚠️ РУЧКА ТОЛЬКО ПРИНИМАЕТ ЗАПРОС, ЗОВЁТ СБОРЩИК И ОТДАЁТ БАЙТЫ. Ни одной
строки сборки файла здесь нет — раскладка репозитория требует держать логику
отдельным модулем.
"""

import datetime as dt
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Response

from app.auth import can_see_all, get_current_user
from app.database import get_pool
from app.exports import report_xlsx
from app.routers.reports import _fetch_report
from app.storage import s3

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/reports", tags=["reports"])

# ⚠️ СРОК ЖИЗНИ ССЫЛКИ БЕРЁТСЯ ИЗ СБОРЩИКА, А НЕ ЗАДАЁТСЯ ЗДЕСЬ ВТОРОЙ РАЗ.
# Файл сам печатает колонку «Снимки действуют до», и если подпись и печать
# разойдутся, бухгалтер получит дату, которой ссылка не соответствует —
# ошибка тихая и заметная только через неделю. Значит число одно на двоих.
ЖИЗНЬ_ССЫЛКИ_НА_СНИМОК = int(report_xlsx.СРОК_ССЫЛКИ_НА_СНИМОК.total_seconds())


def _ссылка_на_снимок(чек: dict, cfg: Optional["s3.S3Config"]) -> str:
    """Адрес снимка для колонки файла — или пусто, если снимка нет.

    ⚠️ ПОДПИСАННАЯ ССЫЛКА, А НЕ НАША РУЧКА `/photo`: ручка требует заголовок
    авторизации, а файл открывают в Excel, где заголовков нет. Ровно этот
    случай назван в докстроке `presign_get_url` («ссылку получает не браузер,
    а сам владелец данных: выгрузка, передача во внешнюю систему»).

    Хранилище не настроено — отдаём пусто, а не роняем всю выгрузку: чек
    без ссылки на снимок остаётся полноценной строкой файла.
    """
    if чек.get("photo_key") and cfg is not None:
        try:
            return s3.presign_get_url(
                cfg, чек["photo_key"], expires_in=ЖИЗНЬ_ССЫЛКИ_НА_СНИМОК
            )
        except Exception:
            # 152-ФЗ: наружу не уходит ни ключ, ни ответ поставщика.
            logger.warning(
                "Не удалось подписать ссылку на снимок, чек id=%s", чек.get("id")
            )
            return ""
    return чек.get("photo_url") or ""


@router.get("/{id}/export.xlsx")
async def export_report_xlsx(id: int, user: dict = Depends(get_current_user)):
    """Отчёт с чеками одним листом xlsx.

    ⚠️ ПРАВО — БУХГАЛТЕР И АДМИНИСТРАТОР (`can_see_all`). Основание: в карте
    ролей платформы у бухгалтера прямо записано «Проверка, 1С выгрузка».
    И довод от кода: список людей сотруднику режется до имени и фамилии, то
    есть под его ролью реквизиты подотчётного лица в файл всё равно не
    попадут. ⚠️ Решение владельцем пока НЕ подтверждено — меняется одной
    строкой, если сотруднику выгрузка своего отчёта всё-таки нужна.

    ⚠️ СТАТУС ОТЧЁТА НЕ ОГРАНИЧИВАЕТСЯ. Сегодня ни одна ручка чтения на него
    не смотрит, и ломать это поведение молча нельзя. Чтобы выгруженный
    черновик не выглядел принятым к учёту, статус стоит КОЛОНКОЙ в самом
    файле, в каждой строке.
    """
    if not can_see_all(user["role"]):
        raise HTTPException(
            status_code=403, detail="Выгрузка доступна бухгалтеру и администратору"
        )

    p = await get_pool()
    отчёт = await _fetch_report(p, id, user)
    # Чужой отчёт неотличим от несуществующего — как во всех ручках отчётов.
    if not отчёт:
        raise HTTPException(status_code=404, detail="Not found")

    # Статья расхода нужна СЛОВОМ, а в чеке лежит только category_id —
    # ни один запрос отчётов справочник не соединяет, поэтому соединяем здесь.
    чеки = await p.fetch(
        """SELECT receipts.*, categories.name AS статья
             FROM report_items ri
             JOIN receipts        ON receipts.id = ri.receipt_id
        LEFT JOIN categories      ON categories.id = receipts.category_id
            WHERE ri.report_id = $1 AND receipts.org_id = $2
         ORDER BY receipts.date, receipts.id""",
        id,
        user["org_id"],
    )

    автор = await p.fetchrow(
        "SELECT first_name, last_name FROM users WHERE id=$1 AND org_id=$2",
        отчёт["user_id"],
        user["org_id"],
    )

    cfg = s3.S3Config.from_env()
    отчёт_словарь = dict(отчёт)
    строки = []
    for чек in чеки:
        ряд = dict(чек)
        ряд["снимок"] = _ссылка_на_снимок(ряд, cfg)
        строки.append(ряд)

    байты = report_xlsx.собрать(
        отчёт_словарь,
        report_xlsx.фамилия_и_инициал(dict(автор) if автор else None),
        строки,
        dt.datetime.now(),
    )
    имя = report_xlsx.имя_файла(отчёт_словарь)
    return Response(
        content=байты,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": 'attachment; filename="' + имя + '"',
            # Выгрузка — персональные данные: ИНН продавцов, ФИО подотчётного
            # лица, ссылки на снимки. В кэше промежуточных узлов им не место.
            "Cache-Control": "no-store",
        },
    )
