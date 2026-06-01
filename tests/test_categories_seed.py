"""Фикс №1 фаза A: справочник категорий — структура данных + seed.

Seed гоняется на FakePool (conftest): нет реального Postgres локально, но
FakePool реализует ровно те запросы, что шлёт seed_default_categories
(INSERT category_groups RETURNING id, INSERT categories, EXISTS-проверка).
Бэкфилл строковых категорий удалён вместе с переходом на вариант B."""
from app.categories_seed import (
    DEFAULT_CATEGORIES,
    TAX_KINDS,
    seed_default_categories,
)


# ─── 1. Структура справочника (без БД) ───
def test_default_categories_structure():
    assert len(DEFAULT_CATEGORIES) == 11, "должно быть 11 групп"
    all_cats = [(g, name, tk) for g, items in DEFAULT_CATEGORIES for (name, tk) in items]
    assert len(all_cats) == 48, "должно быть 48 статей"
    assert len(TAX_KINDS) == 9
    for _, name, tk in all_cats:
        assert tk in TAX_KINDS, f"{name}: недопустимый tax_kind {tk!r}"
    cat_names = [name for _, name, _ in all_cats]
    assert len(cat_names) == len(set(cat_names)), "дубли имён статей"
    group_names = [g for g, _ in DEFAULT_CATEGORIES]
    assert len(group_names) == len(set(group_names)), "дубли имён групп"


# ─── 2. Seed для новой org ───
async def test_seed_creates_groups_and_categories(db):
    n = await seed_default_categories(db, 1)
    assert n == 48
    assert len(db.category_groups) == 11
    assert len(db.categories) == 48
    assert all(c["org_id"] == 1 for c in db.categories)
    assert all(c["is_default"] and c["is_visible"] for c in db.categories)
    gids = {g["id"] for g in db.category_groups}
    assert all(c["group_id"] in gids for c in db.categories)   # FK на группу той же орг


# ─── 3. Идемпотентность (повторный seed — no-op) ───
async def test_seed_idempotent(db):
    first = await seed_default_categories(db, 1)
    second = await seed_default_categories(db, 1)
    assert first == 48 and second == 0
    assert len(db.categories) == 48 and len(db.category_groups) == 11


# ─── 4. Bootstrap для существующих орг (3 — как в проде), per-org изоляция ───
async def test_seed_bootstrap_multiple_orgs(db):
    for org_id in (1, 2, 3):
        await seed_default_categories(db, org_id)
    assert len(db.category_groups) == 33   # 11 × 3
    assert len(db.categories) == 144       # 48 × 3
    for org_id in (1, 2, 3):
        assert sum(1 for c in db.categories if c["org_id"] == org_id) == 48
