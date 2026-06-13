# AOCG-MAP-001 — Инвентаризация backend (aocg-ai-office)

> Снято из реального кода на коммите `71cb87a` (2026-06-10). Только фактическое
> состояние; чего в коде нет — помечено явно. Стек: FastAPI + asyncpg (PostgreSQL).
> Источник истины схемы — `app/database.py` (`init_db`, идемпотентные миграции).

---

## 1. МОДЕЛИ ДАННЫХ

Все прикладные таблицы org-scoped (`org_id`), мультитенантность включена. Ключевые поля:

### `receipts` — чеки (центральная таблица)
- **Базовые:** `id`, `date`, `org` (название), `amount NUMERIC(12,2)`, `payment`, `employee`, `created_at`
- **Источник/сырьё:** `source` (`manual` \| `qr_scan` \| `photo_ocr` \| `fns`), `raw_data JSONB`, `photo_url`
- **Орг-реквизиты (распарсенные):** `org_legal`, `org_brand`, `org_inn`, `address`, `tax_system`
- **Оплата:** `payment_form`, `payment_detail`, `card_last4`, `card_id → cards(id)`
- **Дата/тип:** `datetime TIMESTAMPTZ`, `currency` (RUB), `operation_type`
- **НДС:** `vat_20`, `vat_10`, `vat_0` (NUMERIC)
- **Фискальные:** `kkt_fn` (ФН), `fd_num` (ФД), `fpd` (ФПД), `kkt_serial`, `kkt_rn`, `cashier`
- **Категория:** `category_id → categories(id)` (канон), `category_manual BOOLEAN` (ручной выбор)
- **Мультитенант:** `org_id`
- Уникальность документа: partial-unique индекс `receipts_kkt_fn_fd_unique (kkt_fn, fd_num)` WHERE обе непустые
- ⚠️ Колонки `category` (строка) и `fn` исторические — выведены из обращения (канон: `category_id`, `kkt_fn`)

### `receipt_items` — позиции чека (1 чек → N позиций, каскад)
- `id`, `receipt_id → receipts(id) ON DELETE CASCADE`, `position`, `name`, `quantity`, `price`, `sum`, `vat_rate`

### `reports` — отчёты
- `id`, `title`, `status` (DEFAULT `'Личные'`), `total NUMERIC(12,2)`, `created`, `created_at`, `org_id`
- ⚠️ `total` пишется **с клиента** (не вычисляется сервером — см. §3)

### `report_items` — связка отчёт ↔ чек (M:N)
- `report_id → reports(id) ON DELETE CASCADE`, `receipt_id → receipts(id)`

### `cards` — способы оплаты / карты
- `id`, `name`, `is_default BOOLEAN`, `org_id`, `created_at`
- Сид при пустой таблице: «Личная карта», «Корпоративная карта»

### `categories` — статьи расходов (per-org копии)
- `id`, `org_id`, `group_id → category_groups(id)`, `name`, `tax_kind`, `position`, `is_default`, `is_visible`
- `tax_kind` — CHECK на 9 видов налогового учёта (Материальные/Прочие/Командировочные/Представительские/Реклама/Транспортные/Оплата труда/Налоги и сборы/Не учитываемые)
- Сид: 11 групп / 48 статей на каждую новую орг (`categories_seed.py`)

### `category_groups` — группы статей (per-org)
- `id`, `org_id`, `name`, `position`, UNIQUE `(org_id, name)`

### `users` — пользователи / сотрудники
- `id`, `first_name`, `last_name`, `patronymic`, `email`, `phone`, `inn`, `region`, `employee_id`, `employee_number`
- **Роль/доступ:** `role` (`admin` \| `accountant` \| `employee` \| …), `is_active`, `org_id`
- **Auth:** `password_hash`, `is_email_verified`, `email_verify_token`, `failed_attempts`, `locked_until`, `last_login_at`
- Сид-founder: Алексей Шукалович (`admin`)

### `organizations` — организации (мультитенант)
- `id`, `name`, `inn`, `type` (`person` \| `company`), `owner_id`, `created_at`
- Bootstrap-орг «АОЦГ», к ней привязываются «осиротевшие» строки

### `invite_links` — инвайт-ссылки
- `id`, `token UNIQUE`, `org_id`, `role`, `created_by`, `expires_at` (NULLable — вечные), `max_uses`, `uses_count`, `is_active`

### `revoked_tokens` — отозванные refresh-токены (JWT)
- `id`, `token_hash`, `expires_at`

### `user_consents` — согласия на обработку ПДн (152-ФЗ)
- `id`, `user_id`, `consent_at`, `ip_address`, `policy_version`, `consent_text` (заморожённый текст)

---

## 2. API ENDPOINTS

Сгруппировано по сущностям. `auth: да` = `Depends(get_current_user)`. Все запросы org-scoped (`org_id` из токена, не из тела).

### Аутентификация и организации — `/api`
- `POST /api/auth/register` — регистрация орг / claim passwordless-аккаунта → токены+user+org | auth: нет
- `GET /api/auth/verify-email` — подтверждение email по токену → токены | auth: нет
- `POST /api/auth/login` — вход email/phone+пароль (rate-limit 10/мин) → токены | auth: нет
- `POST /api/auth/refresh` — обновить access по refresh → `{access_token}` | auth: нет
- `POST /api/auth/logout` — отзыв refresh-токена → `{ok}` | auth: нет
- `GET /api/auth/me` — текущий пользователь + организация | auth: да
- `POST /api/invite/create` — создать инвайт-ссылку | auth: да | **роль: admin**
- `GET /api/invite/validate/{token}` — проверить инвайт (без auth) → `{is_valid, role, org_name, …}` | auth: нет
- `POST /api/auth/register-by-invite` — регистрация по инвайту | auth: нет
- `GET /api/invite/list` — активные инвайты орг | auth: да | **роль: admin**
- `DELETE /api/invite/{token}` — деактивировать инвайт | auth: да | **роль: admin**
- `GET /api/egrul/{inn}` — best-effort lookup орг по ИНН в ЕГРУЛ (может вернуть null) | auth: нет

### Пользователи — `/api/users`
- `GET /api/users/` — список активных сотрудников орг | auth: да
- `GET /api/users/me` — свой профиль (+ последнее согласие) | auth: да
- `PATCH /api/users/me` — редактировать свой профиль | auth: да
- `POST /api/users/me/change-password` — смена пароля | auth: да
- `POST /api/users/` — добавить сотрудника (без email) | auth: да
- `PATCH /api/users/{id}` — редактировать сотрудника | auth: да
- `DELETE /api/users/{id}` — soft-delete (`is_active=false`) | auth: да

### Чеки — `/api/receipts`
- `GET /api/receipts/` — все чеки орг по дате DESC | auth: да
- `GET /api/receipts/{id}` — один чек | auth: да
- `GET /api/receipts/{id}/photo` — фото (redirect на `photo_url` или inline base64 из raw_data) | auth: да
- `GET /api/receipts/suggest-payment?org=` — самый частый способ оплаты для орг-названия | auth: да
- `POST /api/receipts/` — создать чек + дедуп (4 ветки) → чек ± `warning.duplicates` | auth: да
- `POST /api/receipts/bulk-delete` — массовое удаление дублей с блокировками → `{deleted, blocked_fns, blocked_in_report}` | auth: да
- `POST /api/receipts/dedupe-cleanup/` — удалить дубли (date+amount+org) → `{deleted, kept}` | auth: да
- `PATCH /api/receipts/{id}` — изменить payment/org/category (резолв `category_id` сервером, флаг `category_manual`) | auth: да
- `DELETE /api/receipts/{id}` — удалить чек + чистка report_items (всегда 200, anti-enumeration) | auth: да

### OCR — `/api/receipts`
- `POST /api/receipts/ocr/` — распознать фото/PDF через Claude Vision → реквизиты+позиции+confidence; при сбое/без ключа → `{confidence: low}` | auth: нет

### Карты — `/api/cards`
- `GET /api/cards/` — карты орг | auth: да
- `POST /api/cards/` — создать карту | auth: да
- `PATCH /api/cards/{id}` — переименовать | auth: да
- `PATCH /api/cards/{id}/default` — сделать default (atomic: одна default на орг) | auth: да
- `DELETE /api/cards/{id}` — удалить | auth: да

### Категории — `/api/categories`
- `GET /api/categories/?visible_only=` — группы + статьи орг | auth: да (чтение открыто всем ролям)
- `POST /api/categories/` — создать пользовательскую статью | auth: да | **роль: admin/accountant**
- `PATCH /api/categories/{id}` — переименовать / сменить tax_kind (не-системные) | auth: да | **роль: admin/accountant**
- `DELETE /api/categories/{id}` — удалить не-системную (409 если есть привязанные чеки) | auth: да | **роль: admin/accountant**
- `PATCH /api/categories/{id}/visibility` — скрыть/показать любую статью | auth: да | **роль: admin/accountant**

### Отчёты — `/api/reports`
- `GET /api/reports/` — все отчёты орг + `receiptIds[]` | auth: да
- `POST /api/reports/` — создать отчёт (title, total, receiptIds[]) | auth: да
- `PATCH /api/reports/{id}` — сменить статус | auth: да

### ФНС-проверка — `/api/fns`
- `POST /api/fns/check` — прокси QR в proverkacheka.com → 200 `{status:ok, org, category, inn, total, items, raw}` / 404 not_found / 503 unavailable | auth: нет

### Согласия — `/api/consent`
- `POST /api/consent/` — записать согласие (user_id, ip, заморожённый текст) | auth: нет
- `GET /api/consent/{user_id}` — последнее согласие или null | auth: нет

### Сервисы/интеграции — `/api/services`
- `GET /api/services/` — статус интеграций (fns / alfabank / anthropic) | auth: нет

**Итого: 46 эндпоинтов.** Защита `get_current_user` — на всех `/api/users`, `/api/receipts` (кроме `/ocr/`), `/api/cards`, `/api/categories`, `/api/reports`, `/api/auth/me`. Без auth: register/login/refresh/logout/verify, invite-validate, register-by-invite, egrul, ocr, fns/check, consent, services.

---

## 3. ГОТОВЫЕ МЕТРИКИ И АГРЕГАТЫ

### Что backend СЧИТАЕТ сейчас (реальные агрегатные запросы)
- **Частый способ оплаты по орг** — `GET /api/receipts/suggest-payment`: `GROUP BY payment ORDER BY COUNT(*) DESC LIMIT 1` для подсказки карты.
- **Поиск групп-дублей** — `POST /api/receipts/dedupe-cleanup/`: `GROUP BY date,amount,org HAVING COUNT(*)>1`.
- **Счётчик чеков в категории** — `DELETE /api/categories/{id}`: `COUNT(*) receipts WHERE category_id` (гард удаления, 409).
- **Следующая позиция статьи** — `MAX(position)+1` при создании категории.

> Это всё. **Полноценной аналитики/дашбордов на backend нет** — никаких сумм по периодам/категориям/сотрудникам сервер не отдаёт. Фронт агрегирует сам из `GET /api/receipts/`.

### Агрегаты, которые НЕ реализованы, но данные для них УЖЕ ЕСТЬ
| Желаемый агрегат | Данные в наличии | Статус |
|---|---|---|
| Сумма расходов за период | `receipts.date` / `datetime` + `amount` | ❌ нет эндпоинта |
| Сумма по категориям | `category_id → categories` + `amount` | ❌ нет эндпоинта |
| Сумма по сотрудникам | `employee` / `employee_id` + `amount` | ❌ нет эндпоинта |
| Свод по налоговому учёту (9 видов расхода) | `categories.tax_kind` + `amount` | ❌ нет эндпоинта |
| Свод НДС (20/10/0) | `vat_20`, `vat_10`, `vat_0` (хранятся) | ❌ нигде не суммируются |
| Сумма по поставщику/ИНН | `org_inn` (+ индекс) + `amount` | ❌ нет эндпоинта |
| Итог отчёта | `report_items` ↔ `receipts.amount` | ⚠️ `reports.total` приходит **с клиента**, сервер не пересчитывает |

> Категоризация для будущих сводов готова: `app/categorization.py` (`auto_categorize_v2`) — чистая функция «название орг → статья» по ключевым триггерам; каждая статья несёт `tax_kind`. То есть привязка чек → статья → вид налогового учёта проставляется, но агрегирующего среза по ней нет.

---

## 4. ЗАГЛУШКИ / В ПЛАНАХ

- **Интеграция ФНС «Мои чеки онлайн»** (`services.py`, `key=fns`) — статус `not_connected`, статический плейсхолдер. Авто-загрузки чеков из личного кабинета ФНС **нет** (есть только ручная проверка QR через `/api/fns/check`).
- **Альфа-Банк API** (`services.py`, `key=alfabank`) — статус `in_progress`, статический плейсхолдер. Импорта банковских операций **нет** в коде.
- **OCR (Claude Vision)** — эндпоинт `POST /api/receipts/ocr/` написан и рабочий, но **гейтится `ANTHROPIC_API_KEY`**: без ключа сразу возвращает `_fallback()` `{confidence: low}` (`ocr.py:264`). Статус в `/api/services` — `active` если ключ есть, иначе `not_configured`. (По контексту проекта — ждёт оплаты Anthropic API.)
- **Текст согласия на ПДн** — `consent.py` хранит `consent_text` с пометкой `[PLACEHOLDER — заменить на финальный текст юриста]`. Механизм рабочий, юридический текст — черновик.
- **Батч-рекатегоризация (Фикс №4)** — колонка `receipts.category_manual` заведена под будущий массовый пересчёт категорий (`WHERE category_manual=FALSE`), но самого батч-эндпоинта/процесса в коде **нет**.
- **Legacy-согласия** — `users.py:24` `TODO(auth-migration)`: ветка `user_consents` с `user_id='local_user'` осталась от до-авторизационной эпохи.
- **`GET /api/egrul/{inn}`** — best-effort, по факту может стабильно возвращать `null` (зависит от внешнего источника).

---

*Документ сгенерирован в режиме «только чтение». Код не изменялся.*
