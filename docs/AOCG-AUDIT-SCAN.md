# AOCG-AUDIT-SCAN — Аудит экрана сканирования чека

> Только чтение, из реального кода. Backend — `aocg-ai-office` @ `71cb87a`,
> frontend — `aocg-ai-office-web/src/App.jsx` (React). Чего в коде нет — помечено.

---

## 1. ЧТО УЖЕ ЕСТЬ

**Главный компонент:** `ScanReceiptModal` ([src/App.jsx:624](../aocg-ai-office-web/src/App.jsx)). Четыре пути ввода — **все рабочие**:

| Путь | Где | Статус |
|---|---|---|
| Живая камера + QR | `startCamera()` [App.jsx:678-728](../aocg-ai-office-web/src/App.jsx) | ✅ работает (html5-qrcode, fps 15, попытка torch/focus) |
| Фото с камеры | `<input capture="environment">` + `pickFile()` [App.jsx:963,820](../aocg-ai-office-web/src/App.jsx) | ✅ работает |
| Фото из галереи/файлов | `<input>` (без capture) + `pickFile()` [App.jsx:964](../aocg-ai-office-web/src/App.jsx) | ✅ работает |
| Ручной ввод реквизитов | `RequisitesSheet` [App.jsx:1898](../aocg-ai-office-web/src/App.jsx) | ✅ работает |

**Обработка фото — двойная:** из фото сначала пытаются вытащить **QR** локально (`decodeQrFromFile`, jsQR, каскад ~5 попыток + бинаризация Otsu, [App.jsx:493-535](../aocg-ai-office-web/src/App.jsx)); если QR нет — отдельная кнопка **«Распознать фото»** шлёт фото на **OCR** ([App.jsx:1031-1035](../aocg-ai-office-web/src/App.jsx)), условная (`{onOcrFile && …}`), т.е. зависит от прокинутого обработчика.

**Заготовка в потоке:** интеграция ФНС-кабинета «Мои чеки онлайн» — закомментирована (см. §6). Сам экран сканирования и 4 пути — не заглушки.

---

## 2. QR

- **Библиотеки:** `html5-qrcode` (живая камера, [App.jsx:4](../aocg-ai-office-web/src/App.jsx)) + `jsQR` (декод QR из файла фото, [App.jsx:5](../aocg-ai-office-web/src/App.jsx)).
- **Инициализация камеры:** `new Html5Qrcode("qr-reader")`, `facingMode:"environment"`, fps 15 ([App.jsx:679-692](../aocg-ai-office-web/src/App.jsx)).
- **Проверка, что это фискальный QR:** `isFiscalQR()` — true только если строка содержит `t=`, `&fn=`, `&fp=` ([App.jsx:462-464](../aocg-ai-office-web/src/App.jsx)); иначе игнор.
- **Парсинг строки:** `parseQRString()` ([App.jsx:206-212](../aocg-ai-office-web/src/App.jsx)) — из `t=дата+время & s=сумма & fn=ФН & i=ФД & fp=ФПД & n=тип` извлекает `{date, amount, fn, fd, fpd, type}`. Дата = `t[0:4]-t[4:6]-t[6:8]`.
- **Куда уходит:** строка → `_fetchFns()` → `POST /api/fns/check` тело `{qr_raw}` ([App.jsx:2026-2039](../aocg-ai-office-web/src/App.jsx)), таймаут ~10с. Бэк проксирует в **proverkacheka.com** (`POST /api/v1/check/get`, [app/routers/fns.py:12,23](app/routers/fns.py#L12)).
- **Маппинг статусов** (`handleCapture`, [App.jsx:2065-2111](../aocg-ai-office-web/src/App.jsx)): `200 + status:"ok" + org` → **ok**; `404` → **not_found**; `503`/таймаут(0) → **unavailable**; иначе → **partial**.
- Префетч: `prefetchFns()` запускает проверку сразу при захвате QR, до подтверждения — к моменту тапа ответ обычно уже готов.

---

## 3. OCR (фото)

- **Frontend:** `handleOcrFile()` ([App.jsx:2116-2154](../aocg-ai-office-web/src/App.jsx)) шлёт `FormData{ file }` на `POST /api/receipts/ocr/`, таймаут ~20с. Из ответа берёт `org`/`amount` (обязательны — иначе `partial`), `date`, `category`, `fn`, `payment_type` → заполняет форму, **source="photo_ocr"**.
- **Backend endpoint:** `POST /api/receipts/ocr/` ([app/routers/ocr.py:231](app/routers/ocr.py#L231)), **без авторизации**.
- **Вход:** один файл `file` — JPEG/PNG/WEBP или **PDF** (первая страница флэттится в JPEG, [ocr.py:256-262](app/routers/ocr.py#L256)). Лимит **5 МБ** ([ocr.py:31](app/routers/ocr.py#L31)).
- **Модель — одна, не каскад:** `claude-haiku-4-5` ([ocr.py:29](app/routers/ocr.py#L29)), Claude Vision (image + `OCR_PROMPT`), `max_tokens 2048`, `max_retries 0`, таймаут **15с** ([ocr.py:30,269-294](app/routers/ocr.py#L269)).
- **Выход:** структурный JSON `_finalize(parsed)` — `org_legal/org_brand/org_inn/address/datetime/amount/currency/operation_type/payment_form/card_last4/tax_system/vat_*/cashier/items/category/confidence/warnings` + эхо `photo_base64` исходного фото ([ocr.py:312-319](app/routers/ocr.py#L318)).
- **Гейтинг `ANTHROPIC_API_KEY`:** если ключа нет → сразу `_fallback()` `{confidence:"low", …}` ([ocr.py:264-266](app/routers/ocr.py#L264)). Тот же fallback на любой сбой (таймаут/APIError/не-JSON/битый PDF) — клиент всегда получает парсимый ответ.

---

## 4. РУЧНОЙ ВВОД

- **Компонент:** `RequisitesSheet` ([App.jsx:1898](../aocg-ai-office-web/src/App.jsx)).
- **Поля формы** ([App.jsx:1901-1907, 1952-1984](../aocg-ai-office-web/src/App.jsx)): `date` (дата), `time` (время), `opType` (тип операции — select), `amount` (сумма), `fn` (ФН, 16 цифр), `fd` (ФД), `fpd` (ФПД). Типы операции `OP_TYPES`: 1 Приход / 2 Возврат прихода / 3 Расход / 4 Возврат расхода ([App.jsx:1887-1892](../aocg-ai-office-web/src/App.jsx)).
- **Сборка строки:** `buildQRString()` ([App.jsx:219-223](../aocg-ai-office-web/src/App.jsx)) — обратная к `parseQRString`: `t=ГГГГММДДTЧЧММ&s=сумма&fn=…&i=ФД&fp=ФПД&n=тип`.
- **Проверка:** собранная строка → `onVerify(qr)` = тот же `handleCapture` ([App.jsx:1920-1932](../aocg-ai-office-web/src/App.jsx)) → `POST /api/fns/check`. Успех → `onClose()`, форма уже заполнена как после скана (**source="qr_scan"**).
- **Фолбэк:** кнопка «Записать без проверки» → `onManualFallback({date, amount})` → `openManualForm()` ([App.jsx:1944-1945, 2167-2170](../aocg-ai-office-web/src/App.jsx)), **source="manual"**, `raw_data=null`.

---

## 5. СОЗДАНИЕ ЧЕКА

Все пути создают чек одним запросом — `POST /api/receipts/` ([App.jsx:2206](../aocg-ai-office-web/src/App.jsx)).

**Тело** ([App.jsx:2199-2209](../aocg-ai-office-web/src/App.jsx)): `{date, org, amount, category, payment, source, kkt_fn?, raw_data?}`.

**Обязательное (бэк, модель `ReceiptIn`, [app/routers/receipts.py:31-42](app/routers/receipts.py#L31)):** `date`, `org`, `amount`. Остальное опционально; `source` по умолчанию `manual`. Эндпоинт **требует авторизации** (`Depends(get_current_user)`), `org_id` берётся из токена.

**Отличия по путям:**
| Путь | `source` | `raw_data` | `kkt_fn` |
|---|---|---|---|
| QR-скан (FNS ok) | `qr_scan` | `body.raw` от ФНС | из QR |
| Ручной ввод (FNS ok) | `qr_scan` | `body.raw` от ФНС | из формы |
| OCR фото | `photo_ocr` | результат OCR (+`photo_base64`) | — (на бэке kkt_fn=NULL) |
| Ручной фолбэк «без проверки» | `manual` | `null` | — |

**Ответы** ([App.jsx:2211-2228](../aocg-ai-office-web/src/App.jsx)): `409` → `dupId` + баннер «Открыть» (точный дубль); `200` → создан, опц. `warning.duplicates` (мягкий дубль); иначе `addError`.

---

## 6. ЗАГЛУШКИ В ПОТОКЕ СКАНИРОВАНИЯ

- **ФНС-кабинет «Мои чеки онлайн» / онлайн-чеки** — **не реализовано**. Вкладка закомментирована (`TODO: … включить когда будет готова интеграция`, [App.jsx:2279-2280](../aocg-ai-office-web/src/App.jsx)). Нет авто-загрузки/поллинга/фоновой синхронизации — чеки попадают только через скан/ввод/OCR.
- **Подключение ФНС в Настройках** — кнопка `disabled title="Скоро"` ([App.jsx:2536-2540](../aocg-ai-office-web/src/App.jsx)); на бэке статус сервиса `fns = "not_connected"` ([app/routers/services.py](app/routers/services.py)).
- **OCR** — код рабочий, но **инактивен без `ANTHROPIC_API_KEY`** (возвращает `confidence:low`-заглушку). На бэке статус `anthropic` = `active`/`not_configured` в зависимости от ключа.
- **Фото → R2:** постоянного хранилища фото нет — фото возвращается как `photo_base64` в `raw_data` (временно, до подключения Cloudflare R2; колонка `receipts.photo_url` под будущий URL, [ocr.py:313-318](app/routers/ocr.py#L313)).

---

*Документ только для чтения. Код не изменялся. Не коммичен.*
