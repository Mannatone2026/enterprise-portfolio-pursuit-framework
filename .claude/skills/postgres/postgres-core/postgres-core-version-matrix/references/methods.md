# postgres-core-version-matrix : methods reference

Full per-version feature table, GUC introduction list, fallback patterns. All entries verified against the official PostgreSQL release notes for 15.0, 16.0, and 17.0.

## Per-version feature matrix

Status legend : `v15 / v16 / v17` columns. `yes` = available, `no` = not available, `extends` = available with new sub-features added in that version.

### SQL surface

| Feature | v15 | v16 | v17 |
|---|---|---|---|
| MERGE statement | yes | extends | extends |
| MERGE RETURNING clause | no | no | yes |
| MERGE WHEN NOT MATCHED BY SOURCE | no | no | yes |
| MERGE on updatable views (with RULES) | no | no | yes |
| JSON_TABLE() | no | no | yes |
| JSON_ARRAY / JSON_OBJECT / JSON_ARRAYAGG / JSON_OBJECTAGG | no | yes | extends |
| JSON_EXISTS / JSON_QUERY / JSON_VALUE / JSON_SCALAR / JSON_SERIALIZE | no | no | yes |
| IS JSON predicate | no | yes | extends |
| jsonpath methods .bigint() / .boolean() / .date() / .timestamp() etc. | no | yes | extends |
| Non-decimal integer literals (0x, 0o, 0b, underscore separators) | no | yes | extends |
| regexp_count / regexp_instr / regexp_like / regexp_substr | yes | extends | extends |
| security_invoker views | yes | extends | extends |

### COPY

| Feature | v15 | v16 | v17 |
|---|---|---|---|
| COPY DEFAULT 'token' | no | yes | extends |
| COPY ON_ERROR ignore | no | no | yes |
| COPY LOG_VERBOSITY verbose | no | no | yes |
| COPY HEADER MATCH | no | no | yes |
| COPY FORCE_NULL (*) all columns | no | no | yes |
| COPY FORCE_NOT_NULL (*) all columns | no | no | yes |

### Logical replication

| Feature | v15 | v16 | v17 |
|---|---|---|---|
| PUBLICATION row filter (WHERE) | yes | extends | extends |
| PUBLICATION column list | yes | extends | extends |
| FOR TABLES IN SCHEMA publication | yes | extends | extends |
| Logical decoding on standby | no | yes | extends |
| Bidirectional logical replication (SUBSCRIPTION ORIGIN) | no | yes | extends |
| Parallel apply (SUBSCRIPTION STREAMING parallel) | no | yes | extends |
| pg_createsubscriber utility | no | no | yes |
| Failover-aware logical slots (WITH failover = true) | no | no | yes |
| sync_replication_slots GUC | no | no | yes |
| synchronized_standby_slots GUC | no | no | yes |
| Logical slot preservation across pg_upgrade | no | no | yes (only for v17+ -> later upgrades) |

### Backup and recovery

| Feature | v15 | v16 | v17 |
|---|---|---|---|
| pg_basebackup --incremental | no | no | yes |
| pg_combinebackup utility | no | no | yes |
| summarize_wal / wal_summary_keep_time GUCs | no | no | yes |
| pg_walsummary utility | no | no | yes |
| pg_basebackup --sync-method | no | no | yes |
| pg_dump --filter | no | yes | extends |
| pg_dump --table-and-children / --exclude-table-and-children / --exclude-table-data-and-children | no | yes | extends |
| pg_dump LZ4 compression | yes | extends | extends |
| pg_dump Zstandard compression | yes | extends | extends |
| WAL LZ4 / Zstandard compression (wal_compression = lz4 / zstd) | yes | extends | extends |

### Security and roles

| Feature | v15 | v16 | v17 |
|---|---|---|---|
| public schema CREATE revoked from PUBLIC (new DBs only) | yes | extends | extends |
| Predefined role pg_checkpoint | yes | extends | extends |
| Predefined role pg_use_reserved_connections | no | yes | extends |
| Predefined role pg_create_subscription | no | yes | extends |
| Predefined role pg_maintain | no | no | yes |
| MAINTAIN per-table privilege | no | no | yes |
| GRANT ... WITH INHERIT clause | no | yes | extends |
| CREATEROLE requires ADMIN OPTION (rewrite) | no | yes | extends |
| Direct TLS handshake (sslnegotiation=direct) | no | no | yes |

### Configuration GUCs (introduction version)

| GUC | Introduced |
|---|---|
| statement_timeout | pre-v15 (mature) |
| lock_timeout | pre-v15 (mature) |
| idle_in_transaction_session_timeout | pre-v15 (mature) |
| idle_session_timeout | pre-v15 (mature, since v14) |
| transaction_timeout | v17 |
| summarize_wal | v17 |
| wal_summary_keep_time | v17 |
| io_combine_limit | v17 |
| sync_replication_slots | v17 |
| synchronized_standby_slots | v17 |
| max_parallel_apply_workers_per_subscription | v16 |
| reserved_connections | v16 |
| wal_compression = lz4 / zstd values | v15 |

### Performance and I/O

| Feature | v15 | v16 | v17 |
|---|---|---|---|
| Parallel hash join FULL and right OUTER | no | yes | extends |
| pg_stat_io system view | no | yes | extends |
| Streaming I/O subsystem (sequential scan, vacuum) | no | no | yes |
| pg_stat_checkpointer view | no | no | yes |
| pg_wait_events catalog | no | no | yes |
| pg_stat_progress_vacuum indexes_total / indexes_processed | no | no | yes |
| VACUUM BUFFER_USAGE_LIMIT (and vacuum_buffer_usage_limit GUC) | no | yes | extends (default raised) |
| VACUUM PROCESS_MAIN | no | yes | extends |
| VACUUM SKIP_DATABASE_STATS / ONLY_DATABASE_STATS | no | yes | extends |

## Version detection helpers

ALWAYS use one of these patterns ; NEVER parse `version()`.

### Server-side : SQL

```sql
-- raw integer (e.g. 170002, 160005, 150010) :
SELECT current_setting('server_version_num')::int;

-- boolean comparison :
SELECT current_setting('server_version_num')::int >= 170000 AS at_least_v17;

-- show metadata :
SHOW server_version;       -- "17.2"
SHOW server_version_num;   -- "170002"
```

### Client-side : libpq

`PQserverVersion(conn)` returns an int formatted as above. Use it for client-driven conditional codepaths.

### Client-side : psql

```
\echo :SERVER_VERSION_NAME    -- "17.2"
\echo :SERVER_VERSION_NUM     -- "170002"
```

### NEVER

```sql
-- WRONG : version() text format is unstable across releases
SELECT split_part(version(), ' ', 2);
```

## Fallback patterns for v17-only features

Each fallback expresses the v15+v16-compatible alternative to a v17-only construct.

### MERGE RETURNING fallback

`MERGE ... RETURNING merge_action(), col*` (v17+) :

Fallback for portable UPSERT-with-RETURNING : `INSERT INTO t (...) VALUES (...) ON CONFLICT (key) DO UPDATE SET ... RETURNING xmax = 0 AS inserted, *` . The `xmax = 0` trick returns true on insert and false on update.

For batch operations needing both INSERT and DELETE paths : run MERGE without RETURNING (or run separate `INSERT ... ON CONFLICT` + `DELETE` statements inside one transaction), then re-read affected rows by primary key.

### MERGE WHEN NOT MATCHED BY SOURCE fallback

`MERGE ... WHEN NOT MATCHED BY SOURCE THEN DELETE` (v17+) :

Fallback : `DELETE FROM target WHERE NOT EXISTS (SELECT 1 FROM source WHERE source.key = target.key)` in a separate statement. Run inside the same transaction as the corresponding MERGE without WHEN NOT MATCHED BY SOURCE.

### JSON_TABLE fallback

`SELECT * FROM JSON_TABLE(jdoc, '$.items[*]' COLUMNS (id INT PATH '$.id', name TEXT PATH '$.name'))` (v17+) :

Fallback : `SELECT (elem->>'id')::int AS id, elem->>'name' AS name FROM jsonb_array_elements(jdoc->'items') AS elem;` works on v15+. For named-column projection : `SELECT * FROM jsonb_to_recordset(jdoc->'items') AS x(id int, name text);` (v15+).

### transaction_timeout fallback

`SET transaction_timeout = '30s'` (v17+) :

Fallback : combine `SET statement_timeout = '30s'` and `SET idle_in_transaction_session_timeout = '30s'`. This does not cover the case where many short statements add up to > 30s in one transaction. There is no fully-equivalent v15+v16 alternative ; if true transaction-wall-clock-limiting is required, enforce it client-side.

### Incremental backup fallback

`pg_basebackup --incremental` (v17+) :

Fallback : use a third-party tool (pgBackRest, Barman, WAL-G) that implements its own incremental backup format on top of WAL archiving. These work on all supported versions.

### pg_createsubscriber fallback

`pg_createsubscriber` (v17+) :

Fallback : promote the physical standby, then run the manual sequence : `CREATE PUBLICATION ... FOR ALL TABLES` on the old primary, `CREATE SUBSCRIPTION ... CONNECTION ... PUBLICATION ...` on the new node, then drop the now-redundant publication after initial sync. Slow and error-prone, which is why v17 introduced the utility.

### pg_maintain role fallback

`GRANT pg_maintain TO role` (v17+) for VACUUM/ANALYZE/REINDEX/REFRESH MATERIALIZED VIEW/CLUSTER without superuser :

Fallback (v15+v16) : object owner can VACUUM/ANALYZE own tables ; otherwise a superuser-owned maintenance function with `SECURITY DEFINER` is the common workaround.

## Sources

- https://www.postgresql.org/docs/release/17.0/
- https://www.postgresql.org/docs/release/16.0/
- https://www.postgresql.org/docs/release/15.0/
- https://www.postgresql.org/docs/17/sql-merge.html
- https://www.postgresql.org/docs/17/sql-copy.html
- https://www.postgresql.org/docs/17/app-pgbasebackup.html
- https://www.postgresql.org/docs/17/app-pgdump.html
- https://www.postgresql.org/docs/17/predefined-roles.html
- https://www.postgresql.org/docs/17/logical-replication.html
- https://www.postgresql.org/docs/17/runtime-config-client.html (transaction_timeout, idle_session_timeout)
- Vooronderzoek section §20 (Version Matrix)
