# AOCG AI Офис — бэкенд

Бэкенд платформы AOCG AI Офис: B2B SaaS для управления первичными финансовыми документами и управленческим учётом для российского малого и среднего бизнеса.

## Стек
- FastAPI (Python)
- PostgreSQL через asyncpg
- JWT-авторизация (bcrypt, refresh-токены)
- Railway (хостинг, auto-deploy из main)
- GitHub Actions (pytest на каждый push)

## Запуск локально
```bash
source venv/bin/activate
uvicorn app.main:app --reload
```
API поднимается на `http://localhost:8000`.

## Тесты
```bash
pytest tests/ -v
```

## Структура
```
app/
├── main.py            FastAPI, CORS, startup, миграции
├── database.py        asyncpg pool, init_db()
├── auth.py            JWT, bcrypt, блокировка попыток
├── categorization.py  авто-категоризация чеков
└── routers/           эндпоинты по доменам
tests/                 pytest + conftest (FakePool)
```

## Переменные окружения
Полный список — в `.env.example`. Реальные значения хранятся в Railway → Variables, в репозиторий не коммитятся.

## Документация
- `docs/development-workflow.md` — цикл разработки
- `docs/prompting-guide.md` — постановка задач агенту
- `docs/claude-md-guide.md` — про CLAUDE.md
- `CLAUDE.md` — постоянные правила для AI-агента

## Безопасность
Проект работает с персональными и финансовыми данными (152-ФЗ). Оператор ПД — ИП Шукалович Алексей Иванович. Перед коммитом работают pre-commit хуки (блокировка секретов). Правила безопасности — в `CLAUDE.md` и проектной документации.

## Деплой
Push в `main` → Railway пересобирает и деплоит автоматически за 1–2 минуты.
