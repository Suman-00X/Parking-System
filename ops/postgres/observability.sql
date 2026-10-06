-- Server-side query observability. Run once as a superuser, then restart Postgres
-- (shared_preload_libraries is only read at start-up):
--   make pg-observe        # applies this file and prints the restart command
--
-- log_min_duration_statement writes every statement slower than 100 ms to the
-- server log, with its duration. The app logs the same threshold as slow_query
-- (RADREPORT_DB__SLOW_QUERY_MS) with the route that ran it; the server log is the
-- one to trust for statements the app did not issue (migrations, psql, cron).
ALTER SYSTEM SET log_min_duration_statement = 100;
ALTER SYSTEM SET shared_preload_libraries = 'pg_stat_statements';
ALTER SYSTEM SET pg_stat_statements.track = 'top';
ALTER SYSTEM SET pg_stat_statements.max = 10000;
ALTER SYSTEM SET track_io_timing = on;
SELECT pg_reload_conf();
