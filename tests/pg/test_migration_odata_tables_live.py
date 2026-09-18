# -*- coding: utf-8 -*-
"""Рельсы валидации таблиц обмена с 1С — ПРОГОН НА ЖИВОЙ БАЗЕ (1C-21, ②).

⚠️ ЗАЧЕМ ОБЁРТКА, А НЕ ПРОСТО СКРИПТ. Рельсы, которые никто не запускает,
молчат о собственной поломке ровно так же, как молчали бы исправные.
У прошлых миграций это уже стоило разбора: `validate_people_fields`
и `validate_invite_columns` живут в репозитории и в прогон не попадают
ни разу.

⚠️ И ГЛАВНОЕ: ТАБЛИЦЫ СНОСЯТСЯ ПЕРЕД ПРОГОНОМ НАМЕРЕННО. `init_db` уже
создал их при подъёме схемы, и без сноса проверялся бы только ПОВТОРНЫЙ
запуск — путь «создаётся с нуля» остался бы непроверенным, а прогон
выглядел бы зелёным. Ровно на этом был забракован первый прогон рельсов
ключей интеграции 12.09.2026.

⚠️ ЧЕГО ЭТА ПРОВЕРКА НЕ ДЕЛАЕТ: она НЕ применяет миграцию на проде. Там
DDL выполнит `init_db` при следующем старте контейнера, и перед этим
владелец прогоняет BEGIN/ROLLBACK на бастионе своими руками.
"""

import importlib.util
import os
import subprocess
import sys

import pytest

СНЕСТИ = (
    "DROP INDEX IF EXISTS org_accounting_profile_unique",
    "DROP TABLE IF EXISTS org_accounting_profile",
    "DROP INDEX IF EXISTS org_expense_kind_map_unique",
    "DROP TABLE IF EXISTS org_expense_kind_map",
    "DROP INDEX IF EXISTS org_category_map_unique",
    "DROP TABLE IF EXISTS org_category_map",
    "DROP INDEX IF EXISTS odata_user_map_unique",
    "DROP TABLE IF EXISTS odata_user_map",
    "DROP INDEX IF EXISTS idx_odata_exports_org",
    "DROP INDEX IF EXISTS odata_exports_one_success",
    "DROP TABLE IF EXISTS odata_exports",
    "DROP INDEX IF EXISTS odata_category_map_unique",
    "DROP TABLE IF EXISTS odata_category_map",
)
РЕЛЬСЫ = "scripts/validate_odata_tables.py"


async def _снести(db):
    for ddl in СНЕСТИ:
        await db.pool.execute(ddl)


def _рельсы():
    """Модуль рельсов: списки таблиц и DDL берём оттуда, не переписываем."""
    сп = importlib.util.spec_from_file_location(
        "рельсы_1с",
        os.path.join(
            os.path.dirname(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            ),
            РЕЛЬСЫ,
        ),
    )
    м = importlib.util.module_from_spec(сп)
    сп.loader.exec_module(м)
    return м


async def _вернуть(db):
    """⚠️ ВОЗВРАТ ОБЯЗАТЕЛЕН, И ЭТО НЕ ВЕЖЛИВОСТЬ. Схема живой базы одна
    на весь прогон: тест, снёсший таблицы и не вернувший их, роняет
    СОСЕДНИЕ проверки, а выглядит это как их собственная поломка.
    Первая редакция так и сделала — упал тест, который ничего не сносил."""
    м = _рельсы()
    for ddl in м.МИГРАЦИЯ:
        await db.pool.execute(ddl)


async def _категория(db):
    """Категория для правила. ⚠️ ЗАВОДИТСЯ, А НЕ ИЩЕТСЯ: пропуск «категорий
    нет» делал бы сторожа молчаливо-зелёным ровно там, где он нужен, —
    а молчаливо-зелёный прогон хуже красного (T87)."""
    await db.добавить_организацию(id=1)
    номер = await db.pool.fetchval("SELECT id FROM categories WHERE org_id=1 LIMIT 1")
    if номер is not None:
        return номер
    группа = await db.pool.fetchval(
        "INSERT INTO category_groups (org_id, name, position)"
        " VALUES (1, 'Проба 1С', 1) RETURNING id"
    )
    return await db.pool.fetchval(
        "INSERT INTO categories (org_id, group_id, name, tax_kind, position)"
        " VALUES (1, $1, 'Проба 1С', 'Прочие расходы', 1) RETURNING id",
        группа,
    )


def _прогнать(адрес):
    корень = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    return subprocess.run(
        [sys.executable, РЕЛЬСЫ],
        cwd=корень,
        capture_output=True,
        text=True,
        timeout=180,
        env={**os.environ, "DATABASE_URL": адрес},
    )


@pytest.mark.asyncio
async def test_рельсы_проходят_на_пустой_схеме(db, адрес_живой_базы):
    """Путь «таблиц нет, создаются с нуля» — тот, что пойдёт на проде."""
    await _снести(db)
    try:
        итог = _прогнать(адрес_живой_базы)
        assert итог.returncode == 0, итог.stdout + итог.stderr
        assert "откат сработал" in итог.stdout, итог.stdout
    finally:
        await _вернуть(db)


@pytest.mark.asyncio
async def test_рельсы_проходят_и_когда_таблицы_уже_есть(db, адрес_живой_базы):
    """Второй путь: `init_db` выполняется на КАЖДОМ старте контейнера."""
    итог = _прогнать(адрес_живой_базы)
    assert итог.returncode == 0, итог.stdout + итог.stderr


@pytest.mark.asyncio
async def test_валидация_НЕ_оставляет_таблиц_за_собой(db, адрес_живой_базы):
    """⚠️ ВАЛИДАЦИЯ, ОСТАВИВШАЯ ТАБЛИЦУ, — ЭТО НЕ ВАЛИДАЦИЯ, А ТИХАЯ
    МИГРАЦИЯ. Проверяем ФАКТОМ: сносим, прогоняем, смотрим в базу."""
    await _снести(db)
    try:
        _прогнать(адрес_живой_базы)
        осталось = await db.pool.fetchval(
            "SELECT count(*) FROM information_schema.tables"
            " WHERE table_name IN ('org_category_map','odata_exports',"
            "'org_accounting_profile')"
        )
        assert осталось == 0, "после валидации в базе осталось таблиц: %s" % осталось
    finally:
        await _вернуть(db)


@pytest.mark.asyncio
async def test_init_db_поднимает_все_таблицы_обмена(db):
    """Схема теста поднимается тем же `init_db`, что и прод.

    ⚠️ СПИСОК БЕРЁТСЯ ИЗ РЕЛЬСОВ, А НЕ ПЕРЕЧИСЛЯЕТСЯ ЗДЕСЬ. Вписанный руками
    список уже врал бы: заход 1C-22 добавил четвёртую таблицу, а тест
    проверял бы три и остался бы зелёным."""
    таблицы = list(_рельсы().ТАБЛИЦЫ)
    есть = await db.pool.fetchval(
        "SELECT count(*) FROM information_schema.tables"
        " WHERE table_name = ANY($1::text[])",
        таблицы,
    )
    assert есть == len(таблицы), "init_db создал %s таблиц из %s" % (есть, len(таблицы))


@pytest.mark.asyncio
async def test_успешная_выгрузка_у_отчёта_ровно_одна(db):
    """⚠️ ИДЕМПОТЕНТНОСТЬ ДЕРЖИТ БАЗА, А НЕ ТОЛЬКО КОД (строка 1C-23).
    Вторая успешная запись по тому же отчёту обязана упасть на индексе —
    даже если код о ней не знает, например при гонке двух выгрузок."""
    import asyncpg

    await db.добавить_организацию(id=1)
    await db.обеспечить_пользователя(id=1, first_name="А", role="admin")
    await db.добавить_отчёт(id=1, title="Июль", user_id=1, org_id=1)
    ид = 1

    async def записать(исход):
        await db.pool.execute(
            "INSERT INTO odata_exports (org_id, report_id, user_id, outcome)"
            " VALUES (1, $1, 1, $2)",
            ид,
            исход,
        )

    await записать("ok")
    with pytest.raises(asyncpg.UniqueViolationError):
        await записать("ok")
    # ⚠️ А НЕУДАЧНЫХ ПОПЫТОК СКОЛЬКО УГОДНО: это история, а не состояние.
    await записать("error")
    await записать("unavailable")
    всего = await db.pool.fetchval(
        "SELECT count(*) FROM odata_exports WHERE report_id=$1", ид
    )
    assert всего == 3


@pytest.mark.asyncio
async def test_подстановка_по_умолчанию_остаётся_в_журнале(db):
    """⚠️ ТРЕБОВАНИЕ ВЛАДЕЛЬЦА ШИРЕ МОМЕНТА ОТВЕТА: через месяц бухгалтер
    спросит, почему всё легло на 20.01, и ответ обязан лежать В ЖУРНАЛЕ.
    Проверяем оба конца: пустой список по умолчанию (подстановок не было)
    и сохранённый перечень (какие категории настроить)."""
    await db.добавить_организацию(id=1)
    await db.обеспечить_пользователя(id=1, first_name="А", role="admin")
    await db.добавить_отчёт(id=2, title="Август", user_id=1, org_id=1)

    # ① Ничего не передали — «правила нашлись для всех», а НЕ «не знаем».
    await db.pool.execute(
        "INSERT INTO odata_exports (org_id, report_id, outcome) VALUES (1, 2, 'ok')"
    )
    пусто = await db.pool.fetchval(
        "SELECT defaulted_categories FROM odata_exports WHERE report_id=2"
    )
    assert пусто == [], "умолчание не пустой список, а %r" % (пусто,)

    # ② Перечень сохраняется целиком и читается без join с категориями.
    await db.pool.execute(
        "INSERT INTO odata_exports (org_id, report_id, outcome, defaulted_categories)"
        " VALUES (1, 2, 'error', $1::text[])",
        ["Такси", "Канцтовары"],
    )
    список = await db.pool.fetchval(
        "SELECT defaulted_categories FROM odata_exports"
        " WHERE report_id=2 AND outcome='error'"
    )
    assert список == ["Такси", "Канцтовары"]


@pytest.mark.asyncio
async def test_колонка_подстановок_не_допускает_неизвестности(db):
    """NULL здесь означал бы «не знаем, были ли подстановки» — а это
    ровно то состояние, ради ухода от которого колонка и заводится."""
    import asyncpg

    await db.добавить_организацию(id=1)
    await db.обеспечить_пользователя(id=1, first_name="А", role="admin")
    await db.добавить_отчёт(id=3, title="Сентябрь", user_id=1, org_id=1)
    with pytest.raises(asyncpg.NotNullViolationError):
        await db.pool.execute(
            "INSERT INTO odata_exports (org_id, report_id, outcome, defaulted_categories)"
            " VALUES (1, 3, 'ok', NULL)"
        )


@pytest.mark.asyncio
async def test_одна_категория_одно_правило(db):
    """Два правила на одну категорию сделали бы проводку зависимой
    от порядка выборки."""
    import asyncpg

    категория = await _категория(db)

    async def правило(счёт):
        await db.pool.execute(
            "INSERT INTO org_category_map (org_id, category_id, account_code,"
            " expense_ref) VALUES (1, $1, $2, 'статья-guid')",
            категория,
            счёт,
        )

    await правило("20.01")
    with pytest.raises(asyncpg.UniqueViolationError):
        await правило("26")


@pytest.mark.asyncio
async def test_один_человек_одно_соответствие(db):
    """⚠️ ДВА СООТВЕТСТВИЯ НА ОДНОГО ЧЕЛОВЕКА означали бы, что подотчётное
    лицо в документе зависит от порядка выборки — а документ уедет в учёт
    клиента, и разбираться с ним будет его бухгалтер."""
    import asyncpg

    await db.добавить_организацию(id=1)
    await db.обеспечить_пользователя(id=1, first_name="А", role="admin")

    async def соответствие(ссылка):
        await db.pool.execute(
            "INSERT INTO odata_user_map (org_id, user_id, person_ref, person_name)"
            " VALUES (1, 1, $1, 'Шукалович А.')",
            ссылка,
        )

    await соответствие("89065214-36a5-11ea-849f-5cb90100870b")
    with pytest.raises(asyncpg.UniqueViolationError):
        await соответствие("другой-элемент-справочника")


@pytest.mark.asyncio
async def test_соответствие_человека_без_ссылки_не_заводится(db):
    """Пустая ссылка — это «соответствие есть, а вести некуда»: запись,
    которая выглядит настройкой и ею не является."""
    import asyncpg

    await db.добавить_организацию(id=1)
    await db.обеспечить_пользователя(id=1, first_name="А", role="admin")
    with pytest.raises(asyncpg.NotNullViolationError):
        await db.pool.execute(
            "INSERT INTO odata_user_map (org_id, user_id, person_ref)"
            " VALUES (1, 1, NULL)"
        )


# ── 1C-22: ПРОФИЛЬ УЧЁТА И ПРАВИЛО КАТЕГОРИИ ─────────────────────────────


@pytest.mark.asyncio
async def test_правило_без_статьи_затрат_не_заводится(db):
    """⚠️ Правило без статьи — не правило (AOCG-1C-001, § 5.2): строка
    описывала бы проводку наполовину, и документ уехал бы со статьёй,
    которую подставит сама 1С."""
    import asyncpg

    категория = await _категория(db)
    with pytest.raises(asyncpg.NotNullViolationError):
        await db.pool.execute(
            "INSERT INTO org_category_map (org_id, category_id, account_code)"
            " VALUES (1, $1, '26')",
            категория,
        )
    with pytest.raises(asyncpg.NotNullViolationError):
        await db.pool.execute(
            "INSERT INTO org_expense_kind_map (org_id, tax_kind)"
            " VALUES (1, 'Прочие расходы')"
        )


@pytest.mark.asyncio
async def test_отражение_в_усн_только_из_трёх_значений(db):
    """⚠️ НАБОР ЗАКРЫТ ДОКУМЕНТОМ: «ВозвратРасхода» — пометка возврата,
    а не правило расхода. Поле документа в 1С объявлено строкой и опечатку
    не отобьёт — значит отбивает база."""
    import asyncpg

    for значение in ("принимаются", "ВозвратРасхода"):
        with pytest.raises(asyncpg.CheckViolationError):
            await db.pool.execute(
                "INSERT INTO org_expense_kind_map (org_id, tax_kind, expense_ref,"
                " usn_reflection) VALUES (1, 'Прочие расходы', 'ст', $1)",
                значение,
            )


@pytest.mark.asyncio
async def test_три_значения_отражения_проходят(db):
    """Пара к тесту выше: CHECK не должен запрещать законное."""
    await db.добавить_организацию(id=1)
    for значение in ("Принимаются", "НеПринимаются", "Распределяются"):
        await db.pool.execute(
            "INSERT INTO org_expense_kind_map (org_id, tax_kind, expense_ref,"
            " usn_reflection) VALUES (1, 'Прочие расходы', 'ст', $1)"
            " ON CONFLICT (org_id, tax_kind) DO UPDATE SET usn_reflection = $1",
            значение,
        )
    лежит = await db.pool.fetchval(
        "SELECT usn_reflection FROM org_expense_kind_map WHERE org_id=1"
    )
    assert лежит == "Распределяются"


@pytest.mark.asyncio
async def test_вид_расхода_только_из_словаря(db):
    """Вид расхода — та же шкала, что у `categories.tax_kind`. Чужое
    значение означало бы правило, к которому не привяжется ни одна
    категория, — молча."""
    import asyncpg

    await db.добавить_организацию(id=1)
    with pytest.raises(asyncpg.CheckViolationError):
        await db.pool.execute(
            "INSERT INTO org_expense_kind_map (org_id, tax_kind, expense_ref)"
            " VALUES (1, 'Придуманный вид', 'ст')"
        )


@pytest.mark.asyncio
async def test_один_вид_расхода_одно_правило(db):
    import asyncpg

    await db.добавить_организацию(id=1)

    async def правило(статья):
        await db.pool.execute(
            "INSERT INTO org_expense_kind_map (org_id, tax_kind, expense_ref)"
            " VALUES (1, 'Прочие расходы', $1)",
            статья,
        )

    await правило("ст-1")
    with pytest.raises(asyncpg.UniqueViolationError):
        await правило("ст-2")


@pytest.mark.asyncio
async def test_режимы_профиля_проверяются_базой(db):
    """Опечатка в режиме тихо сменила бы состав документа: при `included`
    чек не дробится, при `deductible` дробится и НДС не входит в стоимость."""
    import asyncpg

    await db.добавить_организацию(id=1)
    for форма, режим, ндс, политика in (
        ("ooo", "usn", "included", "when_mapped"),
        ("ooo", "usn_dr", "вычет", "when_mapped"),
        ("ао", "usn_dr", "included", "when_mapped"),
        ("ooo", "usn_dr", "included", "иногда"),
    ):
        with pytest.raises(asyncpg.CheckViolationError):
            await db.pool.execute(
                "INSERT INTO org_accounting_profile (org_id, legal_form, tax_regime,"
                " vat_mode, default_account_code, auto_post_policy)"
                " VALUES (1, $1, $2, $3, '26', $4)",
                форма,
                режим,
                ндс,
                политика,
            )


@pytest.mark.asyncio
async def test_патент_и_совмещение_только_у_ип(db):
    """⚠️ СВЯЗКИ ПРОВЕРЯЕТ БАЗА, А НЕ ТОЛЬКО ЭКРАН (§ 5.1). Патент у ООО
    означал бы документ, разнесённый по чужим правилам."""
    import asyncpg

    await db.добавить_организацию(id=1)
    with pytest.raises(asyncpg.CheckViolationError):
        await db.pool.execute(
            "INSERT INTO org_accounting_profile (org_id, legal_form, tax_regime,"
            " vat_mode, default_account_code) VALUES (1, 'ooo', 'psn', 'not_payer', '26')"
        )
    with pytest.raises(asyncpg.CheckViolationError):
        await db.pool.execute(
            "INSERT INTO org_accounting_profile (org_id, legal_form, tax_regime,"
            " vat_mode, combines_psn, default_account_code)"
            " VALUES (1, 'ooo', 'usn_dr', 'included', TRUE, '26')"
        )
    # У ИП то же самое законно.
    await db.pool.execute(
        "INSERT INTO org_accounting_profile (org_id, legal_form, tax_regime,"
        " vat_mode, combines_psn, default_account_code)"
        " VALUES (1, 'ip', 'psn', 'not_payer', TRUE, '26')"
    )


@pytest.mark.asyncio
async def test_профиль_у_организации_ровно_один(db):
    """Два профиля означали бы, что состав документа зависит от порядка
    выборки, — тот же довод, что у правила вида расхода."""
    import asyncpg

    await db.добавить_организацию(id=1)

    async def профиль(счёт):
        await db.pool.execute(
            "INSERT INTO org_accounting_profile (org_id, legal_form, tax_regime,"
            " vat_mode, default_account_code) VALUES (1, 'ooo', 'usn_dr', 'included', $1)",
            счёт,
        )

    await профиль("26")
    with pytest.raises(asyncpg.UniqueViolationError):
        await профиль("44")


@pytest.mark.asyncio
async def test_политика_проведения_по_умолчанию_никогда(db):
    """⚠️ ПЛАТФОРМА ДОКУМЕНТЫ НЕ ПРОВОДИТ (решение владельца 18.09.2026).
    Профиль, заведённый без явного выбора, обязан получить «never»:
    проведение — бухгалтерское действие в чужом учёте, и брать его на себя
    по умолчанию значит решать за бухгалтера клиента."""
    await db.добавить_организацию(id=1)
    await db.pool.execute(
        "INSERT INTO org_accounting_profile (org_id, legal_form, tax_regime,"
        " vat_mode, default_account_code) VALUES (1, 'ooo', 'usn_dr', 'included', '26')"
    )
    политика = await db.pool.fetchval(
        "SELECT auto_post_policy FROM org_accounting_profile WHERE org_id=1"
    )
    assert политика == "never", "умолчание политики проведения — %s" % политика


@pytest.mark.asyncio
async def test_первичный_ключ_переименован_вслед_за_таблицей(db):
    """⚠️ ЛОЖНЫЙ СЛЕД ДОРОЖЕ, ЧЕМ КАЖЕТСЯ. `ALTER TABLE … RENAME` имена
    ограничений не трогает: индекс `odata_category_map_pkey` у таблицы
    `org_category_map` отправит следующий разбор искать таблицу, которой
    в базе нет.

    ⚠️ ПРОВЕРЯЕТСЯ ПУТЬ ПРОДА, А НЕ ЧИСТОЙ БАЗЫ. На чистой базе таблица
    создаётся сразу новым именем, и ключ получает верное имя сам собой —
    такой тест был бы зелёным при снятом переименовании. Поэтому таблица
    СНАЧАЛА возвращается к старому имени, а потом прогоняется миграция,
    как она пройдёт на проде."""
    await db.pool.execute("ALTER TABLE org_category_map RENAME TO odata_category_map")
    await db.pool.execute(
        "ALTER INDEX org_category_map_unique RENAME TO odata_category_map_unique"
    )
    await db.pool.execute(
        "ALTER INDEX org_category_map_pkey RENAME TO odata_category_map_pkey"
    )
    try:
        for ddl in _рельсы().МИГРАЦИЯ:
            await db.pool.execute(ddl)
    finally:
        # Схема живая и одна на прогон: не вернув имена, уроним соседей.
        осталась = await db.pool.fetchval(
            "SELECT 1 FROM information_schema.tables"
            " WHERE table_name = 'odata_category_map'"
        )
        if осталась:
            await db.pool.execute(
                "ALTER TABLE odata_category_map RENAME TO org_category_map"
            )

    имена = {
        з["indexname"]
        for з in await db.pool.fetch(
            "SELECT indexname FROM pg_indexes WHERE tablename = 'org_category_map'"
        )
    }
    assert "org_category_map_pkey" in имена, имена
    assert "odata_category_map_pkey" not in имена, "старое имя индекса осталось"
