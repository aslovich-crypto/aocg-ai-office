"""Фикс №1 фаза B: auto_categorize_v2 (чистая) + resolve_category_id (per-org) +
запись category_id в POST /api/receipts/. resolve/POST гоняются на FakePool со
справочником, засеянным seed_default_categories."""
from app.categories_seed import DEFAULT_CATEGORIES, seed_default_categories
from app.categorization import DEFAULT_FALLBACK, TRIGGERS, auto_categorize_v2
from app.routers.receipts import resolve_category_id


# ─── auto_categorize_v2 — чистая функция ───
def test_v2_known_brand_lukoil():
    assert auto_categorize_v2("Лукойл") == "Топливо"


def test_v2_known_brand_yandex_taxi():
    assert auto_categorize_v2("Яндекс.Такси") == "Такси и каршеринг"


def test_v2_known_brand_hilton():
    assert auto_categorize_v2("Hilton Moscow") == "Командировки — проживание"


def test_v2_known_brand_starbucks():
    assert auto_categorize_v2("Starbucks") == "Кофе и напитки в офис"


def test_v2_case_insensitive():
    assert auto_categorize_v2("ЛУКОЙЛ") == auto_categorize_v2("лукойл") == "Топливо"


def test_v2_substring_in_full_name():
    assert auto_categorize_v2('ООО "Лукойл" АЗС №123') == "Топливо"


def test_v2_no_match_fallback():
    assert auto_categorize_v2("Неизвестный Поставщик XYZ") == DEFAULT_FALLBACK == "Прочие хозрасходы"


def test_v2_empty_fallback():
    assert auto_categorize_v2("") == "Прочие хозрасходы"


def test_v2_magnit_is_products():
    assert auto_categorize_v2("Магнит у дома") == "Продукты для офиса"


def test_v2_all_trigger_keys_are_real_categories():
    # Каждый ключ TRIGGERS — реальное имя статьи из справочника (защита от опечатки),
    # иначе resolve_category_id уйдёт в фолбэк. И сам фолбэк — реальная статья.
    valid = {name for _, items in DEFAULT_CATEGORIES for (name, _) in items}
    assert set(TRIGGERS) <= valid
    assert DEFAULT_FALLBACK in valid


# ─── resolve_category_id — per-org резолв имя → id ───
async def test_resolve_known_category(db):
    await seed_default_categories(db, 1)
    cid = await resolve_category_id(db, 1, "Топливо")
    assert cid == next(c["id"] for c in db.categories if c["org_id"] == 1 and c["name"] == "Топливо")


async def test_resolve_unknown_falls_back_to_prochie(db):
    await seed_default_categories(db, 1)
    cid = await resolve_category_id(db, 1, "Несуществующая статья")
    assert cid == next(c["id"] for c in db.categories if c["org_id"] == 1 and c["name"] == "Прочие хозрасходы")


async def test_resolve_per_org_isolation(db):
    await seed_default_categories(db, 1)
    await seed_default_categories(db, 2)
    c1 = await resolve_category_id(db, 1, "Топливо")
    c2 = await resolve_category_id(db, 2, "Топливо")
    assert c1 != c2
    assert next(c for c in db.categories if c["id"] == c1)["org_id"] == 1
    assert next(c for c in db.categories if c["id"] == c2)["org_id"] == 2


async def test_resolve_unseeded_org_returns_none(db):
    assert await resolve_category_id(db, 999, "Топливо") is None


# ─── POST /api/receipts/ пишет category_id (org_id=1 из client-фикстуры) ───
async def test_post_auto_categorizes_and_writes_category_id(client, db):
    await seed_default_categories(db, 1)
    resp = await client.post("/api/receipts/", json={
        "date": "2026-05-28", "org": "Лукойл АЗС", "amount": 3000.0})
    assert resp.status_code == 200
    body = resp.json()
    assert body["category"] == "Топливо"
    assert body["category_id"] == next(
        c["id"] for c in db.categories if c["org_id"] == 1 and c["name"] == "Топливо")


async def test_post_explicit_category_resolves_id(client, db):
    await seed_default_categories(db, 1)
    resp = await client.post("/api/receipts/", json={
        "date": "2026-05-28", "org": "Кафе", "amount": 500.0, "category": "Обеды сотрудников"})
    body = resp.json()
    assert body["category"] == "Обеды сотрудников"
    assert body["category_id"] == next(
        c["id"] for c in db.categories if c["org_id"] == 1 and c["name"] == "Обеды сотрудников")


async def test_post_unknown_org_fallback_category_id(client, db):
    await seed_default_categories(db, 1)
    resp = await client.post("/api/receipts/", json={
        "date": "2026-05-28", "org": "Неведомый Контрагент", "amount": 100.0})
    body = resp.json()
    assert body["category"] == "Прочие хозрасходы"
    assert body["category_id"] == next(
        c["id"] for c in db.categories if c["org_id"] == 1 and c["name"] == "Прочие хозрасходы")
