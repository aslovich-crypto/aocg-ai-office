"""Фикс №1 фаза B: auto_categorize_v2 (чистая) + resolve_category_id (per-org) +
запись category_id в POST /api/receipts/. resolve/POST гоняются на FakePool со
справочником, засеянным seed_default_categories."""
from app.categories_seed import DEFAULT_CATEGORIES, seed_default_categories
from app.categorization import (
    DEFAULT_FALLBACK,
    ITEM_TRIGGERS,
    TRIGGERS,
    auto_categorize_v2,
    categorize,
    categorize_items,
)
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


# ─── Фикс №4: categorize_items / categorize (по позициям) ───
def _it(name, s):
    return {"name": name, "sum": s}


def test_items_products_win_by_sum():
    # продукты (298+1026) перебивают хозтовары (200) по сумме
    items = [_it("Огурцы Бакинские 350 г", 298), _it("Бедро индейки охл", 1026),
             _it("Туалетная бумага 4 рулона", 200)]
    assert categorize_items(items) == "Продукты для офиса"


def test_items_office_coffee():
    assert categorize_items([_it("Кофе зерновой Lavazza 1кг", 1200)]) == "Кофе и напитки в офис"
    assert categorize_items([_it("Вода питьевая 5 л", 150)]) == "Кофе и напитки в офис"


def test_items_cappuccino_not_matched():
    # Q-B: порционный общепит НЕ ловим — останется фолбэк на магазин
    assert categorize_items([_it("Капучино Гранде 300мл", 420)]) is None
    assert categorize_items([_it("Эспрессо 40мл", 250)]) is None


def test_items_fuel_and_stationery():
    assert categorize_items([_it("АИ-95 32.5 л", 2500)]) == "Топливо"
    assert categorize_items([_it("Бумага А4 SvetoCopy 500л", 350),
                             _it("Степлер Erich Krause", 200)]) == "Канцелярские товары"


def test_items_zero_sums_fall_back_to_count():
    # все суммы нулевые → победитель по числу узнанных (2 продукта vs 1 канцелярия)
    items = [_it("Молоко 1л", 0), _it("Хлеб", 0), _it("Карандаш", 0)]
    assert categorize_items(items) == "Продукты для офиса"


def test_items_unknown_returns_none():
    assert categorize_items([_it("Футболка", 4950), _it("Брюки", 9720)]) is None


def test_items_empty_returns_none():
    assert categorize_items([]) is None
    assert categorize_items(None) is None


def test_item_triggers_keys_are_real_categories():
    # ГАРД (методология №15): каждый ключ ITEM_TRIGGERS — реальное имя статьи
    valid = {name for _, items in DEFAULT_CATEGORIES for (name, _) in items}
    assert set(ITEM_TRIGGERS) <= valid


def test_categorize_items_priority_over_org():
    # «Лукойл» по орг → Топливо, но позиции-продукты перебивают (позиции в приоритете)
    items = [_it("Огурцы 350г", 298), _it("Молоко 1л", 90)]
    assert categorize("Лукойл", items) == "Продукты для офиса"


def test_categorize_falls_back_to_org_when_items_unknown():
    assert categorize("Лукойл", [_it("Футболка", 1000)]) == "Топливо"
    assert categorize("Лукойл", []) == "Топливо"


def test_categorize_falls_back_to_prochie():
    assert categorize("Неизвестный Контрагент", []) == DEFAULT_FALLBACK


async def test_post_categorizes_by_items_azbuka(client, db):
    # Реальный кейс id=3: org=юрлицо (без триггера), но позиции — продукты → Продукты.
    await seed_default_categories(db, 1)
    resp = await client.post("/api/receipts/", json={
        "date": "2026-05-28", "org": 'ООО "Городской супермаркет"', "amount": 1500.0,
        "source": "qr_scan",
        "raw_data": {"user": 'ООО "Городской супермаркет"', "userInn": "7705466989",
                     "items": [{"name": "Огурцы Бакинские 350 г", "sum": 29800},
                               {"name": "Бедро индейки охл Россия", "sum": 102600},
                               {"name": "Пакет майка", "sum": 1290}]}})
    assert resp.status_code == 200
    body = resp.json()
    assert body["category"] == "Продукты для офиса"
    assert body["category_id"] == next(
        c["id"] for c in db.categories if c["org_id"] == 1 and c["name"] == "Продукты для офиса")


# ─── Фикс A2: приоритет бренда (org_brand) над юрлицом ───
def test_v2_brand_priority_over_legal():
    # юрлицо без триггера, но бренд узнаётся → категория по бренду
    assert auto_categorize_v2('ООО "Городской супермаркет"', brand="Азбука Вкуса") == "Продукты для офиса"


def test_v2_brand_unknown_falls_back_to_legal():
    # бренд не узнан → пробуем юрлицо (тот самый OCR-баг: раньше юрлицо не пробовалось)
    assert auto_categorize_v2("Лукойл АЗС №1", brand="Неизвестный Бренд XYZ") == "Топливо"


def test_v2_brand_none_backward_compat():
    # brand=None → поведение как до A2
    assert auto_categorize_v2("Лукойл") == "Топливо"
    assert auto_categorize_v2("Неведомый Контрагент") == DEFAULT_FALLBACK


def test_v2_brand_and_legal_unknown_fallback():
    assert auto_categorize_v2("ООО Ромашка", brand="ИП Иванов") == DEFAULT_FALLBACK


def test_categorize_items_beat_brand():
    # приоритет: позиции > бренд. Канцелярия по позициям перебивает бренд «Лукойл» (топливо)
    items = [{"name": "Бумага А4 SvetoCopy 500л", "sum": 350}]
    assert categorize("Лукойл АЗС", items, brand="Лукойл") == "Канцелярские товары"


def test_categorize_brand_without_items():
    # позиций нет → бренд решает (юрлицо без триггера)
    assert categorize('ООО "Городской супермаркет"', [], brand="Азбука Вкуса") == "Продукты для офиса"


async def test_post_categorizes_by_brand_when_items_unknown(client, db):
    # Фикс A2 end-to-end: юрлицо без триггера + позиции не узнаны, но retailPlace=бренд Азбука
    await seed_default_categories(db, 1)
    resp = await client.post("/api/receipts/", json={
        "date": "2026-05-28", "org": 'ООО "Городской супермаркет"', "amount": 12.90,
        "source": "qr_scan",
        "raw_data": {"user": 'ООО "Городской супермаркет"', "userInn": "7705466989",
                     "retailPlace": 'Супермаркет "Азбука Вкуса"',
                     "items": [{"name": "Пакет майка ТМ", "sum": 1290}]}})
    assert resp.status_code == 200
    body = resp.json()
    assert body["category"] == "Продукты для офиса"
    assert body["category_id"] == next(
        c["id"] for c in db.categories if c["org_id"] == 1 and c["name"] == "Продукты для офиса")
