# AOCG AI Офис — структурная карта (по факту из кода)

> Карта построена **по фактическому коду** двух репозиториев;
> сверена и обновлена **2026-09-03 (T52)** — семь расхождений выправлено.
> Первичный обход — 2026-06-29:
> `aocg-ai-office` (бэкенд, FastAPI) и `aocg-ai-office-web` (фронтенд, React).
> Где функциональность из ТЗ ещё не написана — это помечено стилем
> **«план / не реализовано»**, а не нарисовано как существующее.
>
> Метод: четыре параллельных read-only обхода (роутеры/безопасность,
> схема БД/фото, внешние API/инфра, фронтенд). Ничего не менялось.

---

## 1. Общая карта связей (слои)

```mermaid
flowchart TB
    %% ============ ФРОНТЕНД ============
    subgraph FE["ФРОНТЕНД · aocg-ai-office-web · React 19 + Vite · server.js :4173"]
        direction TB
        subgraph FEFIN["Приложение «Финансы» (active) — App.jsx (монолит ~10.4k строк)"]
            FE_Home["Главная / дашборд<br/>pages/GlavnayaPage.jsx"]
            FE_Checks["Раздел «Чеки» (Operacii)<br/>список + QR-сканер + загрузка фото/OCR"]
            FE_Svodka["Раздел «Сводка»<br/>аналитика, НДС, графики"]
            FE_Reports["Раздел «Отчёты»<br/>Личные/На проверке/Одобрены/Отклонены"]
        end
        FE_Auth["Вход / Регистрация<br/>Login · Register · VerifyEmail · Join"]
        FE_Consent["Согласие 152-ФЗ<br/>ConsentScreen (2 чекбокса)"]
        FE_Settings["Настройки<br/>аккаунт · организация · пользователи/роли ·<br/>категории · карты · сервисы · приглашения"]
        FE_API["API-слой: src/lib/api.js — authFetch<br/>Bearer JWT + авто-refresh на 401 · base = VITE_API_URL<br/>вынесено из монолита: 11 модулей lib/ · 6 components/ · 4 pages/"]

        subgraph FEPLAN["План / НЕ реализовано (только названия в меню)"]
            FE_Prima["Приложение «Прима»:<br/>Авансовые отчёты · Акты · Счета"]
            FE_FinMod["Модули Финансов:<br/>ДДС · ОПУ · Бюджет"]
        end
    end

    FE_Home --> FE_API
    FE_Checks --> FE_API
    FE_Svodka --> FE_API
    FE_Reports --> FE_API
    FE_Auth --> FE_API
    FE_Consent --> FE_API
    FE_Settings --> FE_API

    %% ============ БЭКЕНД ============
    subgraph BE["БЭКЕНД · aocg-ai-office · FastAPI · Timeweb App Platform (Procfile uvicorn :$PORT)"]
        direction TB
        BE_MW["AOCGSecurityMiddleware<br/>rate-limit · IP-бан · HTTPS · security-headers"]
        BE_CORS["CORS whitelist (CORS_ORIGINS)"]
        BE_Auth["auth.py · JWT HS256<br/>access 60м / refresh 30д · bcrypt(12)<br/>lockout 5 попыток / 15м · revoked_tokens"]
        BE_Sec["пакет aocg_security/<br/>маскирование ПД (ИНН/fn/карты) +<br/>валидация ИНН/карт/сумм"]

        subgraph ROUTERS["app/routers/ (11 доменов)"]
            R_auth["auth · invite · egrul<br/>/api/auth · /api/invite · /api/egrul"]
            R_receipts["receipts · ocr<br/>/api/receipts (+ /ocr/, /bulk-delete)"]
            R_fns["fns · /api/fns/check"]
            R_reports["reports · /api/reports"]
            R_cards["cards · /api/cards"]
            R_cat["categories · /api/categories"]
            R_users["users · /api/users"]
            R_org["organizations · /api/organizations"]
            R_consent["consent · /api/consent"]
            R_services["services · /api/services"]
            R_search["search · /api/search (T144: чеки+отчёты)"]
            R_ocr["ocr · /api/receipts/ocr"]
            R_max["max_relay · /internal/max (мост бота, не фронт)"]
        end
        BE_Cat["categorization.py<br/>триггер-словари (локально, без сети)"]
    end

    FE_API -->|"HTTPS + Bearer JWT"| BE_CORS --> BE_MW --> ROUTERS
    BE_Auth -. "get_current_user / org-scope" .-> ROUTERS
    BE_Sec -. "маскирование/валидация" .-> BE_MW
    BE_Sec -.-> R_fns
    R_receipts --> BE_Cat
    R_fns --> BE_Cat

    %% ============ ДАННЫЕ ============
    subgraph DATA["ДАННЫЕ · PostgreSQL @ Timeweb Cloud, Москва (asyncpg pool, init_db)"]
        DB[("12 таблиц<br/>organizations · users · receipts ·<br/>receipt_items · reports · report_items ·<br/>cards · category_groups · categories ·<br/>invite_links · user_consents · revoked_tokens")]
        PHOTO["Фото чека: photo_key → приватный бакет S3 Timeweb<br/>чтение: photo_key → photo_url → base64 (наследие)<br/>подписанная ссылка с коротким сроком"]
    end

    R_receipts --> DB
    R_reports --> DB
    R_cards --> DB
    R_cat --> DB
    R_users --> DB
    R_org --> DB
    R_consent --> DB
    R_auth --> DB
    R_receipts --> PHOTO

    %% ============ ВНЕШНИЕ СЕРВИСЫ ============
    subgraph EXT["ВНЕШНИЕ СЕРВИСЫ"]
        EX_Claude["Claude Vision · Anthropic<br/>claude-haiku-4-5 · OCR чеков<br/>ANTHROPIC_API_KEY"]
        EX_PCheck["proverkacheka.com<br/>проверка чека по QR · PROVERKACHEKA_TOKEN"]
        EX_Egrul["egrul.nalog.ru<br/>контрагент по ИНН (без ключа)"]
        EX_Postbox["Yandex Cloud Postbox · письма по SMTP<br/>верификация/сброс/инвайты · POSTBOX_SMTP_* (РФ)"]
        EX_Sentry["Sentry · мониторинг ошибок · SENTRY_DSN<br/>⚠️ маскирование НЕПОЛНОЕ: raw_data (S-69),<br/>код вокруг падения (S-70)"]
        EX_Yoo["ЮКасса / YooKassa · платежи<br/>НЕ подключено (нет кода)"]
        EX_Alfa["Альфа-Банк · только статус в /api/services<br/>интеграции нет"]
    end

    R_receipts -->|"POST /ocr/"| EX_Claude
    R_fns --> EX_PCheck
    R_auth --> EX_Egrul
    R_search --> DB
    R_ocr --> EX_Claude
    R_auth --> EX_Postbox
    BE_MW -.-> EX_Sentry

    %% ============ ИНФРА ============
    subgraph INFRA["ИНФРА · Timeweb Cloud (auto-deploy из main) + CI"]
        INF_BE["сервис: backend (FastAPI)"]
        INF_FE["сервис: frontend (server.js)"]
        INF_PG["сервис: PostgreSQL"]
        INF_CI["GitHub Actions · pytest + ruff на push"]
        INF_Uptime["UptimeRobot · внешний пинг /health<br/>(без ПД; ложные DOWN — T46)"]
        INF_Yandex["S3 Timeweb Cloud · приватный бакет<br/>фото чеков по photo_key · ФАКТ (задача №3)"]
    end

    INF_BE -. "host" .-> BE_MW
    INF_FE -. "host" .-> FE_API
    INF_PG -. "host" .-> DB
    PHOTO -- "photo_key" --> INF_Yandex

    %% ============ СТИЛИ ============
    classDef transborder fill:#ffe0e0,stroke:#cc0000,stroke-width:2px,color:#000;
    classDef planned fill:#f0f0f0,stroke:#999,stroke-dasharray:5 5,color:#555;
    classDef rf fill:#e0f0ff,stroke:#0066cc,color:#000;

    class EX_Claude,EX_Sentry transborder;
    class EX_Postbox rf;
    class EX_PCheck,EX_Egrul rf;
    class FE_Prima,FE_FinMod,EX_Yoo,EX_Alfa planned;
```

### Легенда

| Обозначение | Смысл |
|---|---|
| 🟥 красная заливка | **Трансграничный канал** (ПД покидают РФ): Claude Vision (США), Resend (ЕС), Sentry (ЕС). Требуют согласия субъекта + уведомления РКН (152-ФЗ) |
| 🟦 голубая заливка | Внешний сервис **внутри РФ**: proverkacheka, ЕГРЮЛ |
| ⬜ серый пунктир | **План / не реализовано** в коде на дату карты |
| сплошная стрелка | реальный вызов / связь в коде |
| пунктирная стрелка | сквозная связь (guard, hosting, маскирование, целевое состояние) |

---

## 2. Схема данных (PostgreSQL) — таблицы и связи

Все доменные таблицы привязаны к `organizations.id` через `org_id`
(мультиарендность; org-scope фильтр обязателен во всех запросах).

```mermaid
erDiagram
    organizations ||--o{ users : "org_id"
    organizations ||--o{ receipts : "org_id"
    organizations ||--o{ reports : "org_id"
    organizations ||--o{ cards : "org_id"
    organizations ||--o{ category_groups : "org_id"
    organizations ||--o{ categories : "org_id"
    organizations ||--o{ invite_links : "org_id"

    users ||--o{ receipts : "user_id (автор, A-ACL)"
    users ||--o{ invite_links : "created_by"

    category_groups ||--o{ categories : "group_id"
    categories ||--o{ receipts : "category_id (канон)"
    cards ||--o{ receipts : "card_id"

    receipts ||--o{ receipt_items : "receipt_id (CASCADE)"
    receipts ||--o{ report_items : "receipt_id"
    reports ||--o{ report_items : "report_id (CASCADE)"

    users ||--o{ user_consents : "user_id (TEXT, аудит)"

    organizations {
        int id PK
        text name
        text inn
        text type "person|company"
        int owner_id FK
        varchar tax_system "СНО"
    }
    receipts {
        int id PK
        int org_id FK
        int user_id FK "автор"
        int category_id FK
        int card_id FK
        date date
        numeric amount
        text source "manual|qr_scan|photo_ocr|fns"
        varchar kkt_fn "фискальный № ККТ"
        varchar fd_num "+ UNIQUE(kkt_fn,fd_num)"
        varchar org_inn "ИНН поставщика"
        bool category_manual "защита ручных правок"
        text photo_url "внешний URL (пока пусто)"
        jsonb raw_data "ответ ФНС/OCR; photo_base64 больше НЕ пишется"
        varchar fd_num "ФД — принимается и от клиента (T132)"
        varchar fpd "ФПД — принимается и от клиента (T132)"
        varchar photo_key "ключ в приватном бакете (№3)"
    }
    receipt_items {
        int id PK
        int receipt_id FK
        varchar name
        numeric quantity
        numeric price
        numeric sum
    }
    reports {
        int id PK
        int org_id FK
        varchar title
        varchar status "Личные|На проверке|..."
        numeric total
    }
    report_items {
        int report_id FK
        int receipt_id FK
    }
    users {
        int id PK
        int org_id FK
        text email
        text password_hash "bcrypt"
        text role "admin|accountant|employee"
        int failed_attempts
        timestamptz locked_until
    }
    cards {
        int id PK
        int org_id FK
        varchar name
        bool is_default
    }
    category_groups {
        int id PK
        int org_id FK
        text name
    }
    categories {
        int id PK
        int org_id FK
        int group_id FK
        text name
        text tax_kind
        bool is_visible
    }
    invite_links {
        int id PK
        text token UK
        int org_id FK
        int created_by FK
        text role
        timestamptz expires_at "NULL = бессрочно"
        text email "кому выписано (T104)"
        text first_name "T104"
        text last_name "T104"
        timestamptz sent_at "когда ушло письмо (T104)"
    }
    %% users: + tokens_valid_from (отзыв токенов без чёрного списка, S-16);
    %% частичный уникальный индекс uq_users_email_lower (T104 этап ①)
    %% report_items: чек живёт ровно в одном отчёте (uq_report_items_receipt_id)
    user_consents {
        int id PK
        text user_id
        timestamptz consent_at
        text ip_address
        text policy_version
        text consent_text "заморожен"
    }
    revoked_tokens {
        int id PK
        text token_hash "SHA256 refresh-токена"
        timestamptz expires_at
    }
```

**Где хранятся фото чеков (по факту, T52 03.09.2026):**
- **Приватный бакет S3 Timeweb Cloud** (РФ): чек ссылается колонкой
  `photo_key`; наружу отдаётся подписанная ссылка с коротким сроком.
- Чтение по приоритету `photo_key → photo_url → base64` — два последних
  как наследие старых строк; **новые записи base64 не содержат**
  (OCR-ответ вычищается до записи). Это и есть «закрытое объектное
  хранилище» из юридических текстов — карта больше им не противоречит.

---

## 3. Поток обработки чека: сканирование → OCR/ФНС → категоризация → БД

Фактически есть **три** пути добавления чека, и они сходятся на одном
INSERT с дедупликацией.

```mermaid
flowchart TD
    Start(["Пользователь в разделе «Чеки»"])

    Start --> Choice{Способ ввода}

    %% --- Ветка QR ---
    Choice -->|"QR-код"| QR["QR-сканер (html5-qrcode/jsqr)<br/>читает qrraw"]
    QR --> FNS["POST /api/fns/check<br/>→ proverkacheka.com 🇷🇺<br/>(retry x2, timeout 10с)"]
    FNS --> FNSparse["parse_fns_response<br/>суммы /100 (копейки→руб)"]

    %% --- Ветка ФОТО ---
    Choice -->|"Фото / PDF"| Photo["Загрузка фото/PDF (≤5 МБ)<br/>PDF: 1-я стр → JPEG (PyMuPDF)"]
    Photo --> OCR["POST /api/receipts/ocr/<br/>→ Claude Vision 🌍 США<br/>claude-haiku-4-5 (timeout 15с)"]
    OCR --> OCRparse["parse_ocr_response → JSON<br/>снимок кладётся в бакет, ответ отдаёт photo_key<br/>(photo_base64 вычищается до записи)"]

    %% --- Ветка РУЧНОЙ ---
    Choice -->|"Вручную"| Manual["Ручной ввод полей"]

    %% --- Слияние: категоризация ---
    FNSparse --> Cat["categorize() · app/categorization.py<br/>триггер-словари по названию/бренду<br/>(локально, без сети)"]
    OCRparse --> Cat
    Manual --> Confirm

    Cat --> Confirm["Предпросмотр: пользователь<br/>подтверждает/правит поля и категорию"]

    Confirm --> Create["POST /api/receipts/<br/>org-scope + user_id (автор)"]
    Create --> Dedup{"Дедуп:<br/>UNIQUE(kkt_fn, fd_num)<br/>+ мягко date+amount+org_inn (7 дн)"}
    Dedup -->|"новый"| Insert[("INSERT в receipts<br/>photo_key — колонкой; raw_data без base64")]
    Dedup -->|"дубль"| Warn["warning.duplicates →<br/>предложить bulk-delete"]

    Insert --> Done(["Чек в списке; виден по A-ACL:<br/>admin/accountant — все, employee — свои"])

    classDef transborder fill:#ffe0e0,stroke:#cc0000,color:#000;
    classDef rf fill:#e0f0ff,stroke:#0066cc,color:#000;
    class OCR transborder;
    class FNS rf;
```

---

## 4. Бэкенд — роутеры и эндпоинты (по группам)

Подключение в `app/main.py` (порядок): auth → receipts → reports → fns →
cards → ocr → consent → users → services → categories → organizations.

| Домен / префикс | Ключевые эндпоинты |
|---|---|
| **auth · invite · egrul** (`/api`) | `POST /auth/register`, `GET /auth/verify-email`, `POST /auth/login` (10/мин), `POST /auth/refresh`, `POST /auth/logout`, `GET /auth/me`, `POST /invite/create`, `GET /invite/validate/{t}`, `POST /auth/register-by-invite`, `GET /invite/list`, `DELETE /invite/{t}`, `GET /egrul/{inn}` |
| **receipts** (`/api/receipts`) | `GET /`, `GET /{id}`, `GET /{id}/photo`, `POST /`, `PATCH /{id}`, `DELETE /{id}` (soft, анти-энумерация), `POST /bulk-delete`, `POST /dedupe-cleanup/`, `GET /suggest-payment` |
| **ocr** (`/api/receipts`) | `POST /ocr/` — распознавание фото/PDF через Claude Vision |
| **fns** (`/api/fns`) | `POST /check` — проверка чека по QR через proverkacheka |
| **reports** (`/api/reports`) | `GET /`, `POST /` (IDOR-проверка receiptIds), `PATCH /{id}` |
| **cards** (`/api/cards`) | `GET /`, `POST /`, `PATCH /{id}`, `PATCH /{id}/default`, `DELETE /{id}` |
| **categories** (`/api/categories`) | `GET /`, `POST /` (admin/accountant), `PATCH /{id}`, `DELETE /{id}`, `PATCH /{id}/visibility` |
| **users** (`/api/users`) | `GET /`, `GET /me`, `PATCH /me`, `POST /me/change-password`, `POST /` (admin), `PATCH /{id}`, `DELETE /{id}` |
| **organizations** (`/api/organizations`) | `GET /me`, `PATCH /me` (admin; org_id из токена, без IDOR) |
| **consent** (`/api/consent`) | `POST /` (иммутабельная запись), `GET /{user_id}` |
| **services** (`/api/services`) | `GET /` — статусы интеграций (ФНС, Анропик/OCR, Альфа-Банк) |

**Безопасность (слой):**
- `AOCGSecurityMiddleware` — rate-limit (60/мин общий, 5/мин на `/api/auth/*`),
  авто-бан IP, принудительный HTTPS, security-заголовки.
- `aocg_security/` (внутри бэкенда) — маскирование ПД в логах/Sentry
  (`mask_inn/card/fn`, `mask_log_dict`), валидация ИНН/карт/сумм.
- `auth.py` — JWT HS256 (access 60 мин / refresh 30 дн), bcrypt,
  lockout 5 попыток → 15 мин, `revoked_tokens` (SHA256), защита от
  энумерации логина, fail-fast при отсутствии `JWT_SECRET_KEY`.
- org-scope фильтр (`WHERE org_id = $N`) и ролевой A-ACL
  (`can_see_all` / `can_delete_any`) во всех доменных запросах.

---

## 5. Внешние сервисы и трансграничные каналы (152-ФЗ)

| Сервис | Назначение | env-ключ | Страна | Канал |
|---|---|---|---|---|
| **Claude Vision** (Anthropic) | OCR фото чеков (`claude-haiku-4-5`) | `ANTHROPIC_API_KEY` | 🌍 США | **трансграничный** |
| **Yandex Cloud Postbox** | письма по SMTP: верификация, сброс, инвайты | `POSTBOX_SMTP_*`, `POSTBOX_FROM` | 🇷🇺 РФ | внутренний |
| **Sentry** | мониторинг ошибок — ⚠️ маскирование НЕПОЛНОЕ (S-69: raw_data; S-70: код вокруг падения) | `SENTRY_DSN` | 🌍 ЕС | **трансграничный** |
| **UptimeRobot** | внешний пинг `/health` (ПД не передаются; ложные DOWN — T46) | — | 🌍 США | мониторинг без ПД |
| **proverkacheka.com** | проверка чека по QR (ФНС) | `PROVERKACHEKA_TOKEN` | 🇷🇺 РФ | внутренний |
| **egrul.nalog.ru** | контрагент по ИНН | — (без ключа) | 🇷🇺 РФ | внутренний |
| ЮКасса / YooKassa | платежи/подписки | — | — | **в коде отсутствует** |
| Альфа-Банк | — | — | — | только строка статуса в `/api/services`, интеграции нет |

> Категоризация, парсинг JSON и валидация ИНН — **локальная логика без сети**.
>
> ⚠️ **T52, юридически значимая правка (03.09.2026):** прежняя карта называла
> Resend (ЕС) действующим трансграничным каналом писем и «base64 вместо
> хранилища» — оба утверждения ОПРОВЕРГАЛИ юридический пакет («трансграничная
> передача почты не осуществляется», «закрытое объектное хранилище»). Факт
> кода: письма — Postbox (РФ), фото — приватный бакет S3 Timeweb. Из
> трансграничных остаются Claude Vision (США, закрывается S-44/S-45)
> и Sentry (ЕС, S-20).

---

## 6. Инфраструктура и деплой

- **Хостинг:** Timeweb Cloud, App Platform, auto-deploy из ветки `main` (с 18.08.2026, S-06).
  - Бэкенд: `Procfile` → `uvicorn app.main:app --host 0.0.0.0 --port $PORT`.
  - Фронтенд: `server.js` (Express static + SPA-fallback), слушает **порт 4173**
    (хардкод, не `$PORT`), запуск `npm run start`; сборка `vite build` → `dist/`.
  - PostgreSQL: отдельная база Timeweb Cloud (Москва), строка `DATABASE_URL`.
- **CI:** GitHub Actions на каждый push/PR — `pytest tests/ -v` + `ruff check app/`
  (Python 3.11). Тесты на `FakePool` (зеркало PostgreSQL в `conftest.py`).
- **Секреты:** только панель Timeweb → Переменные (`JWT_SECRET_KEY`, `ANTHROPIC_API_KEY`,
  `PROVERKACHEKA_TOKEN`, `RESEND_API_KEY`, `SENTRY_DSN`, `CORS_ORIGINS`, …).
  `.env` локально пуст, есть `.env.example`.
- **Резидентность (152-ФЗ):** БД и фото — в РФ, Timeweb Cloud (Москва),
  с 18.08.2026. ⚠️ ПОПРАВКА 27.08 (T65): прежняя строка «сейчас на Railway
  (вне РФ, переходно)» была неверна три месяца и читалась как разрешение.
  Целевое — Timeweb Cloud (управляемый PostgreSQL + S3, РФ), задача **S-06**
  (площадка выбрана владельцем 11.08.2026 по сравнению трёх,
  `docs/AOCG-INFRA-001-Sravnenie_ploshchadok_RF.md`).
  Cloudflare R2 и иные зарубежные хранилища ПД — запрещены.

---

## 7. Что из ТЗ ещё НЕ реализовано (на дату карты)

- **Приложение «Прима»** (Авансовые отчёты, Акты, Счета) — отсутствует в коде.
  Реально работает только приложение **«Финансы»** с разделами
  **Чеки, Сводка, Отчёты, Главная**.
- **Модули Финансов ДДС / ОПУ / Бюджет** — только подпись в меню переключателя.
- **Загрузка фото в объектное хранилище** — НАПИСАНА и работает
  (photo_key, приватный бакет; T52 03.09.2026).
- **ЮКасса / платежи** — интеграции нет.
- Приложения «Документы» и «Инструменты» в переключателе — заглушки `soon`.

> Карта отражает фактический код. При появлении новых разделов/интеграций
> (особенно с ПД и трансграничной передачей) — обновлять эту карту вместе с кодом.
