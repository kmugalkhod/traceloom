-- Idempotent upgrades for installs created before a schema change. Run after clickhouse_schema.sql
-- on every start, so every statement must be safe to repeat. Table names are database-qualified
-- at run time.

-- Phase 2: prompt metrics filter observations by prompt_name.
ALTER TABLE observations ADD INDEX IF NOT EXISTS idx_prompt_name coalesce(prompt_name, '') TYPE bloom_filter(0.01) GRANULARITY 4;
