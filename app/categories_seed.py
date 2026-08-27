"""Засев справочника категорий расходов в базу (Фикс №1, фаза A).

ДАННЫЕ ЗДЕСЬ БОЛЬШЕ НЕ ЖИВУТ. Словарь (9 видов расхода, 11 групп, 48 статей)
переехал в ЕДИНЫЙ ИСТОЧНИК `app/dictionaries/categories.json` и попадает сюда
генерацией (`tools/gen_dictionaries.py`), потому что тот же словарь нужен ещё
пяти местам, и три из них — не python: js-фронт, SQL CHECK в `app/database.py`,
промпт распознавания. Раньше все копии писались руками и совпадали по памяти
(разбор — T39 в docs/TASKS.md).

`TAX_KINDS` и `DEFAULT_CATEGORIES` ре-экспортируются ниже НАМЕРЕННО: по ним
уже импортируют `app/routers/categories.py`, `app/database.py`,
`app/routers/auth.py` и пять тестовых файлов. Ломать эти импорты ради переезда
данных не нужно — адрес остаётся прежним, меняется происхождение.

Категория чека хранится в receipts.category_id → categories.id (канон;
старая строковая колонка receipts.category удалена, вариант B).
"""

from app.dictionaries import DEFAULT_CATEGORIES, TAX_KINDS

__all__ = ["DEFAULT_CATEGORIES", "TAX_KINDS", "seed_default_categories"]


async def seed_default_categories(conn, org_id) -> int:
    """Идемпотентно засеять 11 групп + 48 статей для org_id.

    No-op, если у орг уже есть категории (защита от повторного seed при рестарте
    площадкой или ручном вызове). Возвращает число созданных статей (0 при skip).
    Вызывается внутри уже открытой транзакции (регистрация / init_db)."""
    exists = await conn.fetchval(
        "SELECT EXISTS(SELECT 1 FROM categories WHERE org_id=$1)", org_id
    )
    if exists:
        return 0
    created = 0
    for gpos, (gname, items) in enumerate(DEFAULT_CATEGORIES, start=1):
        gid = await conn.fetchval(
            "INSERT INTO category_groups (org_id, name, position) VALUES ($1,$2,$3) RETURNING id",
            org_id,
            gname,
            gpos,
        )
        for cpos, (cname, tax_kind) in enumerate(items, start=1):
            await conn.execute(
                """INSERT INTO categories
                       (org_id, group_id, name, tax_kind, position, is_default, is_visible)
                   VALUES ($1,$2,$3,$4,$5,TRUE,TRUE)""",
                org_id,
                gid,
                cname,
                tax_kind,
                cpos,
            )
            created += 1
    return created
