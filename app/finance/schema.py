#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""DDL таблиц `fin_*` и засев справочника CoA (FIN-01).

⚠️ ЭТОТ КОД ВЫПОЛНЯЕТСЯ ПРИ КАЖДОМ СТАРТЕ КОНТЕЙНЕРА — `init_finance_schema`
зовётся из `init_db()`. Ошибка здесь роняет не только Финансы, но и Приму,
поэтому каждая команда идемпотентна: `CREATE TABLE/INDEX IF NOT EXISTS`,
UNIQUE — только уникальным индексом, не `ADD CONSTRAINT`.

⚠️ `DDL` — ЕДИНСТВЕННЫЙ ИСТОЧНИК ТЕКСТА МИГРАЦИИ. Валидационный скрипт
`scripts/validate_fin_coa_tables.py` ИМПОРТИРУЕТ этот кортеж, а не переписывает
его рядом. Поэтому «проверяли одно, а применили другое» здесь невозможно
структурно, и сторожа на совпадение двух копий не нужно: копии нет.

⚠️ CHECK ВНУТРИ `CREATE TABLE IF NOT EXISTS` РАБОТАЕТ ТОЛЬКО НА НОВОЙ ТАБЛИЦЕ.
На существующей команда — no-op, и ограничение не появится. Таблица новая,
поэтому сейчас это верно; будущая правка перечисления — отдельным `ALTER`,
а не правкой этой строки (тот же приём, что у дефолта `reports.status`).

ОБРАТНЫЙ DDL (точка отката, выполнять в этом порядке):
    DROP INDEX IF EXISTS idx_fin_coa_org;
    DROP INDEX IF EXISTS ux_fin_coa_org_code;
    DROP TABLE IF EXISTS fin_coa_articles;
"""

from __future__ import annotations

from app.finance.coa_seed import seed_coa_for_org

DDL = (
    """
    CREATE TABLE IF NOT EXISTS fin_coa_articles (
        id           SERIAL PRIMARY KEY,
        org_id       INTEGER NOT NULL REFERENCES organizations(id),
        code         TEXT NOT NULL,
        name_ru      TEXT NOT NULL,
        name_en      TEXT,
        section      TEXT NOT NULL CHECK (section IN ('I','II','III','IV','V','VI','VII','VIII')),
        kind         TEXT NOT NULL CHECK (kind IN ('income','expense','balance','distribution')),
        is_cash      BOOLEAN NOT NULL DEFAULT TRUE,
        is_group     BOOLEAN NOT NULL DEFAULT FALSE,
        is_reserve   BOOLEAN NOT NULL DEFAULT FALSE,
        is_active    BOOLEAN NOT NULL DEFAULT TRUE,
        note         TEXT,
        position     INTEGER NOT NULL DEFAULT 0,
        coa_version  TEXT NOT NULL DEFAULT '3.12',
        created_at   TIMESTAMPTZ DEFAULT NOW()
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_fin_coa_org_code
        ON fin_coa_articles(org_id, code)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_fin_coa_org ON fin_coa_articles(org_id)
    """,
)

# Смысл колонок, которые не читаются по имени. Пояснения стоят ЗДЕСЬ, а не
# внутри DDL-строк: комментарий внутри разбираемой конструкции ломает разбор
# молча и в чужом месте (правило CLAUDE.md, куплено четыре раза за два дня).
#   code        — иерархический: '2.5.1', '3.1.1.12'
#   is_cash     — FALSE у неденежной статьи: в ДДС она не идёт (одна, 3.1.1.12)
#   is_group    — заголовочная строка справочника (2.5, 3.1.1 …), не для ввода сумм
#   is_reserve  — пустая статья «Резерв», оставлена для будущих статей бюро
#   is_active   — статьи НЕ удаляются, а деактивируются: на них ссылаются бюджеты
#   position    — порядок показа, 1..94 в порядке справочника-источника
#   coa_version — версия справочника, из которого строка засеяна


async def init_finance_schema(conn) -> None:
    """Схема Финансов: DDL + засев справочника CoA каждой организации.

    Зовётся из `init_db()` один раз за старт. Засев идёт по ВСЕМ организациям
    и для уже засеянных — no-op (тот же приём, что у `seed_default_categories`).
    """
    for оператор in DDL:
        await conn.execute(оператор)

    async with conn.transaction():
        org_ids = [r["id"] for r in await conn.fetch("SELECT id FROM organizations")]
        for org_id in org_ids:
            await seed_coa_for_org(conn, org_id)
