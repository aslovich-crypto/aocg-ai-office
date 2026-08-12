#!/usr/bin/env python3
"""ПЕРЕЛИВ СНИМКОВ ИЗ БАЗЫ В ХРАНИЛИЩЕ — шаг 1 задачи S-40.

ЧТО ДЕЛАЕТ. Берёт чеки, у которых снимок лежит в `raw_data.photo_base64`,
кладёт его в бакет и записывает `photo_key`. **base64 НЕ ТРОГАЕТ ВООБЩЕ** —
удаление это отдельный шаг 3, отдельным заходом, после живой проверки.
Правило репозитория: если проверка возможна только МЕЖДУ двумя правками,
это два коммита; здесь ровно тот случай — пока оригинал в базе, ошибку
перелива можно исправить повторным запуском, после удаления нечем.

КЛЮЧ ДЕТЕРМИНИРОВАННЫЙ: receipts/{org_id}/migrated-{id}.jpg.
Не uuid4, как у новых снимков, и это НЕ небрежность: при обрыве и повторном
запуске uuid4 создал бы ВТОРОЙ объект, `photo_key` перезаписался бы,
а первый остался бы сиротой — то есть S-48 с другого конца. Детерминированный
ключ перезаписывает тот же объект. `org_id` в пути обязателен: без него
в бакете появятся два правила именования, и через год это будет читаться
как следы чужой работы. Слово `migrated` оставлено намеренно — оно честно
говорит о происхождении объекта.

ПРОВЕРКА ПОБАЙТНАЯ, А НЕ «ОБЪЕКТ ПОЯВИЛСЯ». После записи объект скачивается
обратно, считается md5 и длина, и они сравниваются с исходными. Не совпало —
`photo_key` НЕ записывается и работа ОСТАНАВЛИВАЕТСЯ на этом чеке.
Останов на первом расхождении, а не «пропустим и поедем»: на двух чеках
разницы нет, но привычка правильная.

ПОВТОРНЫЙ ЗАПУСК ПРОДОЛЖАЕТ, А НЕ НАЧИНАЕТ ЗАНОВО: выбираются только чеки
с `photo_key IS NULL`, и ключ пишется после КАЖДОГО чека, не пачкой —
обрыв стоит максимум одного чека.

ЗАПУСК (из прод-контейнера, где есть DATABASE_URL и ключи хранилища):
    python scripts/backfill_photos.py --dry-run   # ничего не меняет
    python scripts/backfill_photos.py             # переносит

Код возврата: 0 — всё перенесено и сверено; 1 — расхождение или отказ.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import os
import sys

import asyncpg

from app.storage import s3

SQL_КАНДИДАТЫ = """
    SELECT id, org_id, source, date, org,
           raw_data->>'photo_base64' AS base64
      FROM receipts
     WHERE raw_data ? 'photo_base64'
       AND photo_key IS NULL
     ORDER BY id
"""


def ключ(org_id: int, receipt_id: int) -> str:
    return "receipts/%d/migrated-%d.jpg" % (org_id, receipt_id)


async def main(сухой: bool) -> int:
    cfg = s3.S3Config.from_env()
    if cfg is None:
        print("✗ хранилище не настроено — переменные S3_* не видны. Останов.")
        return 1
    print("хранилище: %s / %s" % (cfg.endpoint, cfg.bucket))
    print("режим: %s\n" % ("СУХОЙ ПРОГОН, ничего не меняем" if сухой else "БОЕВОЙ"))

    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        rows = await conn.fetch(SQL_КАНДИДАТЫ)
        print("чеков к переносу: %d\n" % len(rows))
        if not rows:
            print("переносить нечего — все снимки уже в хранилище")
            return 0

        перенесено = 0
        for r in rows:
            k = ключ(r["org_id"], r["id"])
            try:
                данные = base64.b64decode(r["base64"], validate=True)
            except (binascii.Error, ValueError):
                print("✗ id=%s — base64 в базе битый, останов" % r["id"])
                return 1
            md5_до = hashlib.md5(данные).hexdigest()
            print(
                "id=%-4s org=%-3s %-9s %-24s %7.1f кБ  md5=%s"
                % (
                    r["id"],
                    r["org_id"],
                    r["source"],
                    (r["org"] or "")[:24],
                    len(данные) / 1024,
                    md5_до,
                )
            )
            print("      ключ: %s" % k)

            if сухой:
                print("      сухой прогон: запись и сверка пропущены\n")
                continue

            try:
                await s3.put_object(cfg, k, данные, content_type="image/jpeg")
            except s3.StorageError as e:
                print("      ✗ хранилище не приняло объект: %s. Останов." % e)
                return 1

            try:
                назад, _ = await s3.get_object(cfg, k)
            except s3.StorageError as e:
                print(
                    "      ✗ объект не читается обратно: %s. Останов."
                    % type(e).__name__
                )
                return 1

            md5_после = hashlib.md5(назад).hexdigest()
            if md5_после != md5_до or len(назад) != len(данные):
                print(
                    "      ✗ РАСХОЖДЕНИЕ: было %s (%d б), стало %s (%d б). "
                    "photo_key НЕ записан, останов."
                    % (md5_до, len(данные), md5_после, len(назад))
                )
                return 1
            print("      ✓ сверено побайтно: md5 совпал, длина совпала")

            await conn.execute(
                "UPDATE receipts SET photo_key=$1 WHERE id=$2 AND photo_key IS NULL",
                k,
                r["id"],
            )
            перенесено += 1
            print("      ✓ photo_key записан\n")

        if сухой:
            print(
                "сухой прогон окончен: к переносу %d чек(ов), ничего не изменено"
                % len(rows)
            )
        else:
            print("перенесено и сверено: %d из %d" % (перенесено, len(rows)))
            if перенесено != len(rows):
                return 1
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main("--dry-run" in sys.argv)))
