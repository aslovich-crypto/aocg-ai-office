"""Прогон ВСЕХ запросов приложения через настоящий PostgreSQL (T36, пункт 1).

ЗАЧЕМ. FakePool разбирает SQL строковым сравнением: он примет запрос
с опечаткой в имени колонки, с несуществующей таблицей, с `GROUP BY`,
который PostgreSQL отвергнет. Такая ошибка сегодня видна ТОЛЬКО на проде —
и хуже того, `init_db()` выполняется при каждом старте контейнера, поэтому
битый DDL роняет приложение целиком.

ЧТО ДЕЛАЕТ. Собирает каждый литеральный SQL из `app/` и просит PostgreSQL
ПОДГОТОВИТЬ его (`prepare`). Подготовка разбирает запрос и сверяет с
каталогом — имена таблиц и колонок, типы, синтаксис, — но НЕ ВЫПОЛНЯЕТ.
Ни одной строки не читается и не пишется.

ПОЧЕМУ ИМЕННО prepare, а не выполнение: значения параметров подбирать
не нужно (их 1–9 у разных запросов, с разными типами), побочных эффектов
нет по определению, и прогон занимает секунды.

ЧЕГО ЭТОТ ЗАМЕР НЕ ДЕЛАЕТ, честно:
  • не проверяет СМЫСЛ запроса (правильный ли фильтр) — это T34/T35;
  • не проверяет поведение хендлеров и настоящий ROLLBACK — это пункт 2
    задачи T36, полный переход тестов на настоящую базу;
  • не видит SQL, собранный из переменной или f-строки (в проекте таких
    нет, кроме динамического UPDATE в `PATCH /me`, где колонки берутся
    из белого списка).

ДВА РЕЖИМА, и разница между ними — вопрос безопасности:
  • `--schema` (для CI): поднимает схему через `init_db()` на ПУСТОЙ базе.
    Выполняет DDL, поэтому запускать только на одноразовой базе;
  • без флага (по умолчанию): НИЧЕГО не создаёт, готовит запросы к уже
    существующей схеме. В этом режиме прогон безопасен против прода —
    он не пишет и не читает данные.

ЗАПУСК:
    # CI (пустая база из сервиса postgres)
    DATABASE_URL=postgresql://... ./venv/bin/python tests/tools/sql_check.py --schema
    # локально против прод-схемы, read-only — строка подключения берётся
    # из панели Timeweb, ВНЕШНИМ (публичным) адресом базы
    DATABASE_PUBLIC_URL=postgresql://... ./venv/bin/python tests/tools/sql_check.py
"""

import asyncio
import importlib.util
import os
import pathlib
import sys

import asyncpg

КОРЕНЬ = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(КОРЕНЬ))

_спец = importlib.util.spec_from_file_location(
    "mirror_gaps", pathlib.Path(__file__).with_name("mirror_gaps.py")
)
mirror_gaps = importlib.util.module_from_spec(_спец)
_спец.loader.exec_module(mirror_gaps)

# Запросы, которые prepare не берёт по устройству, а не из-за ошибки.
# Список ЯВНЫЙ: молча пропускать нельзя, иначе «всё зелено» скроет дыру.
ПРОПУСК = {
    # DDL (CREATE/ALTER) не готовится — он и так выполняется в --schema.
    "DDL": lambda sql: sql.upper().startswith(("CREATE ", "ALTER ", "DROP ")),
}


def _пропустить(sql):
    for имя, правило in ПРОПУСК.items():
        if правило(sql):
            return имя
    return None


async def main():
    # ПУБЛИЧНЫЙ адрес ПЕРВЫМ: у площадки внутренний хост базы снаружи не
    # резолвится вовсе, и прогон падал на DNS. Куплено на Railway
    # (`postgres.railway.internal` из `railway run`); у Timeweb внутренний
    # адрес свой, но свойство то же. В CI публичного адреса нет — сработает второй.
    url = os.environ.get("DATABASE_PUBLIC_URL") or os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("нет DATABASE_URL / DATABASE_PUBLIC_URL — базу неоткуда взять")
    поднять_схему = "--schema" in sys.argv

    запросы = mirror_gaps._sql_из_приложения()
    if not запросы:
        sys.exit("разобрано НОЛЬ запросов — сломан сбор, а не «нечего проверять»")

    conn = await asyncpg.connect(url)
    try:
        if поднять_схему:
            # init_db() поднимает схему ТЕМ ЖЕ кодом, что и прод при старте
            # контейнера, — значит заодно проверяется и сам DDL. Пул берёт
            # адрес из DATABASE_URL, поэтому подставляем его явно: в CI он
            # уже такой, но при запуске с публичным адресом — нет.
            import app.database as database

            os.environ["DATABASE_URL"] = url
            database.pool = None
            await database.init_db()
            print("схема поднята через init_db()\n")

        ошибки, проверено, пропущено = [], 0, 0
        for файл, строка, метод, sql in запросы:
            причина = _пропустить(sql)
            if причина:
                пропущено += 1
                continue
            try:
                await conn.prepare(sql)
                проверено += 1
            except Exception as e:  # noqa: BLE001 — интересен текст, не тип
                ошибки.append((файл, строка, метод, sql, f"{type(e).__name__}: {e}"))
    finally:
        await conn.close()

    print(f"Подготовлено запросов: {проверено}; пропущено (DDL): {пропущено}")
    if ошибки:
        print(f"\n── PostgreSQL ОТВЕРГ {len(ошибки)} ЗАПРОС(ОВ) ──")
        for файл, строка, метод, sql, текст in ошибки:
            print(f"\n  {файл}:{строка} ({метод})\n    {текст}\n    SQL: {sql[:140]}")
        sys.exit(1)
    print("✓ все запросы приняты настоящим PostgreSQL")


asyncio.run(main())
