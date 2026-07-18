-- 0009: structured capture fields (hypothesis / next_action / thread_id)
alter table findings add column if not exists metadata jsonb;
