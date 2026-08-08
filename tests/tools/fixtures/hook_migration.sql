-- ОПОРА ДЛЯ ЗАМЕРА hook_probe.py. НЕ ЗАПУСКАТЬ — это данные, а не миграция.
-- Случай A18: тот же регламент в SQL-виде. Прямой DDL исполняется,
-- обратный записан комментарием: откат — DROP INDEX IF EXISTS idx_receipts_org;
CREATE INDEX IF NOT EXISTS idx_receipts_org ON receipts(org_id);
