#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ВАЛИДАЦИЯ МИГРАЦИИ «таблицы обмена с 1С» — BEGIN/ROLLBACK, БЕЗ ЗАПИСИ.

⚠️ ЗАХОД 1C-22 ДОБАВИЛ ЧЕТВЁРТУЮ ТАБЛИЦУ И ДВА ОГРАНИЧЕНИЯ-ПЕРЕЧИСЛЕНИЯ:
профиль учёта организации (режим налогообложения, режим НДС, счёт по
умолчанию), признак принятия в УСН у правила категории и обязательная
статья затрат. Списки ниже — те же, что в `init_db()`, дословно.

⚠️ ЗАКРЕПЛЯЮЩЕЙ КОМАНДЫ ЗДЕСЬ НЕТ НИ ОДНОЙ — только откат. Транзакция
открывается, DDL прогоняется на ЖИВОЙ схеме, состав замеряется через
`information_schema`, и всё откатывается. База остаётся ровно такой же,
какой была. Требование главы 13а.4, оно не смягчается.

⚠️ DDL ПРОГОНЯЕТСЯ ДВАЖДЫ, И ВТОРОЙ РАЗ ВАЖНЕЕ ПЕРВОГО. Первый проверяет
СИНТАКСИС, второй — ИДЕМПОТЕНТНОСТЬ: `init_db()` выполняется при КАЖДОМ
старте контейнера. Ошибка идемпотентности не видна на пустой базе
и роняет приложение на втором старте — уже на проде.

⚠️ СПИСОК ОДИН — ИНАЧЕ ПРОВЕРЯЛИ БЫ ОДНО, А ПРИМЕНЯЛИ ДРУГОЕ. Те же
строки дословно лежат в `init_db()`; `--sql` печатает текст ИЗ ЭТОГО ЖЕ
списка, а не переписывает его рядом. Совпадение стережёт
`tests/test_migration_odata_tables.py`.

⚠️ С БАСТИОНА ПИТОНОМ ЭТОТ ФАЙЛ НЕ ЗАПУСТИТЬ, И ЭТО ЗАМЕР, А НЕ ДОГАДКА:
репозитория на бастионе нет (`/root/aocg-ai-office` не существует), там
база и psql, но не код. Поэтому ключ `--sql` обязателен.

ЗАПУСК:
    · локально, на живом кластере:  python3 scripts/validate_odata_tables.py
    · на бастионе:                  python3 scripts/validate_odata_tables.py --sql
      печатает SQL целиком, дальше вставкой в psql либо `psql "$DATABASE_URL" -f -`

КОДЫ ВОЗВРАТА: 0 — миграция безопасна · 1 — НЕ ГОДИТСЯ · 2 — откат не сработал.
"""

import asyncio
import os
import sys

# ⚠️ ДРАЙВЕР БАЗЫ ИМПОРТИРУЕТСЯ ТОЛЬКО ТАМ, ГДЕ ИДЁТ ПОДКЛЮЧЕНИЕ, — НЕ ЗДЕСЬ.
# Печать SQL (`--sql`) нужна ВЛАДЕЛЬЦУ на его машине, где `asyncpg` нет и не
# будет: он берёт текст и несёт его на бастион. Импорт вверху файла ронял
# `--sql` ещё до печати — `ModuleNotFoundError`, то есть прибор нельзя было
# запустить ровно там, где он нужен. Стережёт это
# `tests/test_migration_odata_tables.py::test_печать_sql_работает_без_драйвера_базы`.

ТАБЛИЦЫ = (
    "org_category_map",
    "org_expense_kind_map",
    "org_accounting_profile",
    "odata_exports",
    "odata_user_map",
)

# ⚠️ ДОСЛОВНАЯ КОПИЯ ТОГО, ЧТО ЛЕЖИТ В `init_db()`.
МИГРАЦИЯ = [
    """ALTER TABLE IF EXISTS odata_category_map RENAME TO org_category_map""",
    """ALTER INDEX IF EXISTS odata_category_map_unique
    RENAME TO org_category_map_unique""",
    """ALTER INDEX IF EXISTS odata_category_map_pkey
    RENAME TO org_category_map_pkey""",
    """CREATE TABLE IF NOT EXISTS org_category_map (
    id            SERIAL PRIMARY KEY,
    org_id        INTEGER NOT NULL REFERENCES organizations(id),
    category_id   INTEGER NOT NULL REFERENCES categories(id),
    account_code  TEXT,
    expense_ref   TEXT NOT NULL,
    expense_name  TEXT,
    usn_reflection TEXT NOT NULL DEFAULT 'Принимаются'
        CHECK (usn_reflection IN ('Принимаются', 'НеПринимаются',
                                  'Распределяются')),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
)""",
    """CREATE UNIQUE INDEX IF NOT EXISTS org_category_map_unique
    ON org_category_map(org_id, category_id)""",
    """ALTER TABLE org_category_map
    ADD COLUMN IF NOT EXISTS usn_reflection TEXT NOT NULL DEFAULT 'Принимаются'
        CHECK (usn_reflection IN ('Принимаются', 'НеПринимаются',
                                  'Распределяются'))""",
    """ALTER TABLE org_category_map ALTER COLUMN account_code DROP NOT NULL""",
    """ALTER TABLE org_category_map ALTER COLUMN expense_ref SET NOT NULL""",
    """CREATE TABLE IF NOT EXISTS org_expense_kind_map (
    id             SERIAL PRIMARY KEY,
    org_id         INTEGER NOT NULL REFERENCES organizations(id),
    tax_kind       TEXT NOT NULL
        CHECK (tax_kind IN (
            'Материальные расходы','Прочие расходы','Командировочные расходы',
            'Представительские расходы','Расходы на рекламу (нормируемые)',
            'Транспортные расходы','Оплата труда','Налоги и сборы',
            'Не учитываемые в целях налогообложения'
        )),
    expense_ref    TEXT NOT NULL,
    expense_name   TEXT,
    account_code   TEXT,
    usn_reflection TEXT NOT NULL DEFAULT 'Принимаются'
        CHECK (usn_reflection IN ('Принимаются', 'НеПринимаются',
                                  'Распределяются')),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
)""",
    """CREATE UNIQUE INDEX IF NOT EXISTS org_expense_kind_map_unique
    ON org_expense_kind_map(org_id, tax_kind)""",
    """CREATE TABLE IF NOT EXISTS org_accounting_profile (
    id                   SERIAL PRIMARY KEY,
    org_id               INTEGER NOT NULL REFERENCES organizations(id),
    legal_form           TEXT NOT NULL
        CHECK (legal_form IN ('ooo', 'ip')),
    tax_regime           TEXT NOT NULL
        CHECK (tax_regime IN ('osno', 'usn_dr', 'usn_d',
                              'ausn_dr', 'ausn_d', 'eshn', 'psn')),
    vat_mode             TEXT NOT NULL
        CHECK (vat_mode IN ('not_payer', 'included', 'deductible')),
    combines_psn         BOOLEAN NOT NULL DEFAULT FALSE,
    default_account_code TEXT NOT NULL,
    auto_post_policy     TEXT NOT NULL DEFAULT 'never'
        CHECK (auto_post_policy IN ('when_mapped', 'never', 'always')),
    created_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT org_accounting_profile_psn_only_ip
        CHECK (tax_regime <> 'psn' OR legal_form = 'ip'),
    CONSTRAINT org_accounting_profile_combines_only_ip
        CHECK (NOT combines_psn OR legal_form = 'ip')
)""",
    """CREATE UNIQUE INDEX IF NOT EXISTS org_accounting_profile_unique
    ON org_accounting_profile(org_id)""",
    """CREATE TABLE IF NOT EXISTS odata_exports (
    id           SERIAL PRIMARY KEY,
    org_id       INTEGER NOT NULL REFERENCES organizations(id),
    report_id    INTEGER NOT NULL REFERENCES reports(id),
    user_id      INTEGER REFERENCES users(id),
    started_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at  TIMESTAMPTZ,
    outcome      TEXT NOT NULL,
    doc_ref      TEXT,
    doc_number   TEXT,
    error_note   TEXT,
    defaulted_categories TEXT[] NOT NULL DEFAULT '{}'
)""",
    """CREATE UNIQUE INDEX IF NOT EXISTS odata_exports_one_success
    ON odata_exports(report_id) WHERE outcome = 'ok'""",
    """CREATE INDEX IF NOT EXISTS idx_odata_exports_org
    ON odata_exports(org_id, started_at DESC)""",
    """CREATE TABLE IF NOT EXISTS odata_user_map (
    id          SERIAL PRIMARY KEY,
    org_id      INTEGER NOT NULL REFERENCES organizations(id),
    user_id     INTEGER NOT NULL REFERENCES users(id),
    person_ref  TEXT NOT NULL,
    person_name TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
)""",
    """CREATE UNIQUE INDEX IF NOT EXISTS odata_user_map_unique
    ON odata_user_map(org_id, user_id)""",
    """ALTER TABLE odata_exports
    ADD COLUMN IF NOT EXISTS defaulted_categories TEXT[] NOT NULL DEFAULT '{}'""",
]

# ОБРАТНЫЙ DDL — точка отката. Выполняется РУКАМИ, приложением никогда.
ОБРАТНЫЙ_DDL = [
    "DROP INDEX IF EXISTS org_accounting_profile_unique",
    "DROP TABLE IF EXISTS org_accounting_profile",
    "DROP INDEX IF EXISTS org_expense_kind_map_unique",
    "DROP TABLE IF EXISTS org_expense_kind_map",
    "DROP INDEX IF EXISTS odata_user_map_unique",
    "DROP TABLE IF EXISTS odata_user_map",
    "DROP INDEX IF EXISTS idx_odata_exports_org",
    "DROP INDEX IF EXISTS odata_exports_one_success",
    "DROP TABLE IF EXISTS odata_exports",
    "DROP INDEX IF EXISTS org_category_map_unique",
    "DROP TABLE IF EXISTS org_category_map",
]

# Что обязано появиться. Список — слепок DDL выше, а не вторая правда:
# тест сверяет его с самим DDL построчно.
ОЖИДАЕМЫЕ_КОЛОНКИ = {
    "org_category_map": (
        "id",
        "org_id",
        "category_id",
        "account_code",
        "expense_ref",
        "expense_name",
        "usn_reflection",
        "created_at",
        "updated_at",
    ),
    "org_expense_kind_map": (
        "id",
        "org_id",
        "tax_kind",
        "expense_ref",
        "expense_name",
        "account_code",
        "usn_reflection",
        "created_at",
        "updated_at",
    ),
    "org_accounting_profile": (
        "id",
        "org_id",
        "legal_form",
        "tax_regime",
        "vat_mode",
        "combines_psn",
        "default_account_code",
        "auto_post_policy",
        "created_at",
        "updated_at",
    ),
    "odata_exports": (
        "id",
        "org_id",
        "report_id",
        "user_id",
        "started_at",
        "finished_at",
        "outcome",
        "doc_ref",
        "doc_number",
        "error_note",
        "defaulted_categories",
    ),
    "odata_user_map": (
        "id",
        "org_id",
        "user_id",
        "person_ref",
        "person_name",
        "created_at",
        "updated_at",
    ),
}
ОЖИДАЕМЫЕ_ИНДЕКСЫ = (
    "odata_user_map_unique",
    "org_category_map_unique",
    "org_expense_kind_map_unique",
    "org_accounting_profile_unique",
    "odata_exports_one_success",
    "idx_odata_exports_org",
)


def напечатать_sql() -> None:
    """SQL для бастиона: BEGIN, миграция, замер, ROLLBACK. Ни одного COMMIT."""
    print(
        "-- ВАЛИДАЦИЯ МИГРАЦИИ «таблицы обмена с 1С» (1C-21 ②③ · 1C-22 (AOCG-1C-001 v0.1))"
    )
    print("-- ⚠️ ЗАКРЕПЛЕНИЯ НЕТ: в конце ROLLBACK, база останется как была.")
    print("BEGIN;")
    for команда in МИГРАЦИЯ:
        print(команда.strip() + ";")
    print("-- повтор целиком: init_db выполняется на КАЖДОМ старте контейнера")
    for команда in МИГРАЦИЯ:
        print(команда.strip() + ";")
    # ⚠️ ИМЕНА ТАБЛИЦ БЕРУТСЯ ИЗ СПИСКА, А НЕ ПЕРЕПИСЫВАЮТСЯ РЯДОМ. Тот же
    # класс, что и вписанные руками числа ниже: четвёртая таблица 1C-22 не
    # попала бы в замер, и владелец сверял бы вывод psql по трём из четырёх.
    перечень = ",".join("'%s'" % т for т in ТАБЛИЦЫ)
    print(
        "SELECT table_name, count(*) AS колонок FROM information_schema.columns\n"
        " WHERE table_name IN (%s)\n"
        " GROUP BY table_name ORDER BY table_name;" % перечень
    )
    print(
        "SELECT indexname FROM pg_indexes\n"
        " WHERE tablename IN (%s)\n"
        " ORDER BY indexname;" % перечень
    )
    # Ограничения-перечисления 1C-22: они и есть предмет этой миграции.
    print(
        "SELECT conname FROM pg_constraint\n"
        " WHERE contype = 'c' AND conrelid::regclass::text IN (%s)\n"
        " ORDER BY conname;" % перечень
    )
    print("ROLLBACK;")
    # ⚠️ ЧИСЛА СЧИТАЮТСЯ, А НЕ ВПИСЫВАЮТСЯ РУКАМИ. Первая редакция обещала
    # «odata_exports 10 колонок», и после добавления `defaulted_categories`
    # печатный хвост стал врать — а по нему владелец сверяет вывод psql.
    # Вписанное руками число живёт ровно до следующей правки DDL.
    for таблица, колонки in ОЖИДАЕМЫЕ_КОЛОНКИ.items():
        print("-- Ожидается: %s — %d колонок" % (таблица, len(колонки)))
    # ⚠️ ПЕРВИЧНЫХ КЛЮЧЕЙ СТОЛЬКО, СКОЛЬКО ТАБЛИЦ, А НЕ «ДВА». Редакция
    # после захода ② уже считала названные индексы, но число первичных
    # ключей оставила вписанным — и с третьей таблицей хвост обещал 6 индексов
    # там, где psql покажет 7. Тот же класс, что и прошлая правка этого места.
    ключей = len(ОЖИДАЕМЫЕ_КОЛОНКИ)
    print(
        "-- индексов %d: %d первичных ключа плюс %d названных."
        % (ключей + len(ОЖИДАЕМЫЕ_ИНДЕКСЫ), ключей, len(ОЖИДАЕМЫЕ_ИНДЕКСЫ))
    )


async def прогнать(адрес: str) -> int:
    import asyncpg  # здесь, а не вверху файла — см. пояснение у импортов

    соединение = await asyncpg.connect(адрес)
    try:
        # ⚠️ СНИМАЕМ СОСТОЯНИЕ ДО НАЧАЛА, И ЭТО НЕ ПЕДАНТИЗМ. Откат обязан
        # вернуть базу в то состояние, какое БЫЛО, а не в пустое: на проде
        # `init_db` уже создавал эти таблицы при прошлом старте, и они там
        # есть законно. Первая редакция сравнивала с нулём и кричала «откат
        # не сработал» на совершенно исправной базе — поймано живым тестом
        # `test_рельсы_проходят_и_когда_таблицы_уже_есть`.
        было = {
            з["table_name"]
            for з in await соединение.fetch(
                "SELECT table_name FROM information_schema.tables"
                " WHERE table_name = ANY($1::text[])",
                list(ТАБЛИЦЫ),
            )
        }
        if было:
            print("  (таблицы уже есть в базе: %s)" % ", ".join(sorted(было)))
        сделка = соединение.transaction()
        await сделка.start()
        try:
            for заход in (1, 2):
                for команда in МИГРАЦИЯ:
                    await соединение.execute(команда)
                print("  прогон %d: DDL выполнен" % заход)

            for таблица, колонки in ОЖИДАЕМЫЕ_КОЛОНКИ.items():
                есть = {
                    з["column_name"]
                    for з in await соединение.fetch(
                        "SELECT column_name FROM information_schema.columns"
                        " WHERE table_name = $1",
                        таблица,
                    )
                }
                нет = [к for к in колонки if к not in есть]
                if нет:
                    print("  ✗ %s: не появились колонки %s" % (таблица, ", ".join(нет)))
                    return 1
                print("  ✓ %s: колонок %d" % (таблица, len(есть)))

            индексы = {
                з["indexname"]
                for з in await соединение.fetch(
                    "SELECT indexname FROM pg_indexes WHERE tablename = ANY($1::text[])",
                    list(ТАБЛИЦЫ),
                )
            }
            нет = [и for и in ОЖИДАЕМЫЕ_ИНДЕКСЫ if и not in индексы]
            if нет:
                print("  ✗ не появились индексы: %s" % ", ".join(нет))
                return 1
            print("  ✓ индексы на месте: %s" % ", ".join(ОЖИДАЕМЫЕ_ИНДЕКСЫ))
        finally:
            await сделка.rollback()

        # ⚠️ ПРОВЕРЯЕМ, ЧТО ОТКАТ ВЕРНУЛ БАЗУ В ПРЕЖНЕЕ СОСТОЯНИЕ. Валидация,
        # оставившая за собой НОВУЮ таблицу, — это не валидация, а тихая
        # миграция. Сравниваем со снимком «до», а не с нулём.
        стало = {
            з["table_name"]
            for з in await соединение.fetch(
                "SELECT table_name FROM information_schema.tables"
                " WHERE table_name = ANY($1::text[])",
                list(ТАБЛИЦЫ),
            )
        }
        появились = стало - было
        if появились:
            print(
                "  ✗✗ ОТКАТ НЕ СРАБОТАЛ: за проверкой остались %s"
                % ", ".join(sorted(появились))
            )
            return 2
        print("  ✓ откат сработал: база как была")
        return 0
    finally:
        await соединение.close()


def main() -> int:
    if "--sql" in sys.argv:
        напечатать_sql()
        return 0
    адрес = os.getenv("DATABASE_URL", "").strip()
    if not адрес:
        print("DATABASE_URL не задан. Запускать с бастиона либо с ключом --sql.")
        return 1
    print("\nВАЛИДАЦИЯ «таблицы обмена с 1С»: BEGIN → DDL ×2 → замер → ROLLBACK\n")
    return asyncio.run(прогнать(адрес))


if __name__ == "__main__":
    sys.exit(main())
