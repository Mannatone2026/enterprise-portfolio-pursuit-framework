# postgres-core-version-matrix : anti-patterns

Every anti-pattern below has been triggered in real PostgreSQL deployments where authors assumed cross-version compatibility but the code targeted v15 or v16 with a v17-only construct (or vice versa). Each entry has : cause, symptom (with SQLSTATE if applicable), fix.

## AP-1 : MERGE ... RETURNING on v15 or v16

CAUSE : Author saw a v17 example or tutorial that used `MERGE ... RETURNING merge_action(), col*` and copied it onto a v15 or v16 instance.

SYMPTOM : `ERROR: syntax error at or near "RETURNING"` (SQLSTATE 42601). Statement is rejected at parse time, no rows modified.

FIX :
- If RETURNING affected rows from a single-row UPSERT : switch to `INSERT ... ON CONFLICT ... DO UPDATE ... RETURNING ...` (works on all supported versions).
- If RETURNING is critical for a batch MERGE : require v17+, or run MERGE without RETURNING followed by a SELECT of the affected primary keys in the same transaction.
- Add a runtime guard : `IF current_setting('server_version_num')::int >= 170000 THEN ... ELSE ...`.

## AP-2 : MERGE ... WHEN NOT MATCHED BY SOURCE on v15 or v16

CAUSE : v17 added the `WHEN NOT MATCHED BY SOURCE` clause for full-sync patterns. Authors targeting older versions assumed it was a MERGE feature since v15.

SYMPTOM : `ERROR: syntax error at or near "BY"` (SQLSTATE 42601).

FIX : split the operation into MERGE (for matched/not-matched-by-target rows) followed by a separate `DELETE FROM target WHERE NOT EXISTS (SELECT 1 FROM source WHERE source.key = target.key)` inside the same transaction.

## AP-3 : JSON_TABLE on v15 or v16

CAUSE : v17 introduced JSON_TABLE as a SQL-standard table function for projecting JSON arrays into rows. Used widely in v17 tutorials.

SYMPTOM : `ERROR: function json_table(jsonb, unknown) does not exist` (SQLSTATE 42883) or `ERROR: syntax error at or near "JSON_TABLE"` (SQLSTATE 42601), depending on form used.

FIX :
- v15+v16 alternative for typed projection : `SELECT * FROM jsonb_to_recordset(jdoc->'items') AS x(id int, name text);`
- v15+v16 alternative for flexible projection : `SELECT (e->>'id')::int AS id, e->>'name' AS name FROM jsonb_array_elements(jdoc->'items') AS e;`

## AP-4 : SET transaction_timeout on v15 or v16

CAUSE : v17 added the `transaction_timeout` GUC. Author saw it in a v17 best-practices blog and added it to shared application config.

SYMPTOM : `ERROR: unrecognized configuration parameter "transaction_timeout"` (SQLSTATE 22023 or 42704 depending on context). Application connection initialization fails for every new connection.

FIX :
- Guard the SET with a version check : `IF current_setting('server_version_num')::int >= 170000 THEN SET transaction_timeout = '30s'; END IF;`
- On v15+v16 the closest approximation is `SET statement_timeout = '30s'; SET idle_in_transaction_session_timeout = '30s';` but this does NOT bound total transaction wall-clock time across multiple statements.

## AP-5 : Parsing version() text instead of server_version_num

CAUSE : Author wanted runtime version detection and wrote `split_part(version(), ' ', 2)`.

SYMPTOM : Behavior depends on PostgreSQL build : community builds emit `PostgreSQL 17.2 on ...`, Debian builds emit `PostgreSQL 17.2 (Debian 17.2-1.pgdg120+1) on ...`, AWS RDS / Aurora variants emit different prefixes. Code that parses position breaks silently when running on a different build.

FIX : ALWAYS use `current_setting('server_version_num')::int` (server-side) or `PQserverVersion(conn)` (client-side, libpq). Both are stable integers across all builds.

## AP-6 : Creating tables in public on v15+ as a non-superuser, fresh database

CAUSE : Author was on v14 or earlier where `public` granted CREATE to PUBLIC. On v15+ this default was revoked for freshly-created databases (CVE-2018-1058 mitigation). Author migrated app to a new v15+ DB.

SYMPTOM : `ERROR: permission denied for schema public` (SQLSTATE 42501) on the first `CREATE TABLE` by the app role. Migration tooling appears broken.

FIX :
- Either : `GRANT CREATE ON SCHEMA public TO app_user;`
- Or (preferred) : `CREATE SCHEMA app AUTHORIZATION app_user;` then `ALTER ROLE app_user SET search_path = app, public;`
- For existing databases pg_upgraded from v14, the prior PUBLIC grant is preserved. The issue only surfaces on a freshly created v15+ database.

## AP-7 : Restoring a v17 incremental backup directory directly

CAUSE : Author took an incremental backup via `pg_basebackup --incremental` on v17, then attempted to start PostgreSQL pointing at the incremental directory.

SYMPTOM : PostgreSQL fails to start. Log shows missing data file errors or `incremental backup` markers in the manifest.

FIX : ALWAYS combine the full backup and all incrementals with `pg_combinebackup` first :

```bash
pg_combinebackup -o /restore/data /backup/full /backup/incr1 /backup/incr2
```

The output of `pg_combinebackup` is a valid data directory ready for postmaster start. Validate with `pg_verifybackup /restore/data` before starting.

## AP-8 : Logical decoding on a v15 standby

CAUSE : Author wanted to offload publisher load from primary to standby. Saw a v16 release-notes mention of "logical decoding on standby" and tried it on v15.

SYMPTOM : `ERROR: logical decoding requires a non-hot-standby` or similar message. `pg_create_logical_replication_slot()` fails.

FIX : Logical decoding on standby is v16+. On v15, either upgrade to v16+, or run the publication on the primary.

## AP-9 : pg_dump --filter on v15

CAUSE : Author saw `pg_dump --filter=` in a tutorial and tried it against a v15 server (or with v15 client binaries).

SYMPTOM : `pg_dump: error: unrecognized option '--filter'`. Dump aborts immediately. (The server can be any version ; what matters is the pg_dump client binary major version.)

FIX : v16+ pg_dump binary is required for `--filter`. Replace with explicit `--table` / `--exclude-table` / `--schema` flags, or upgrade the client tools to v16+ (always safe to use newer pg_dump against an equal-or-older server major version, never the reverse).

## AP-10 : Using pg_createsubscriber on a v15 or v16 standby

CAUSE : Author copy-pasted a v17 conversion runbook onto an older standby.

SYMPTOM : `pg_createsubscriber: command not found` (utility doesn't exist) on v15+v16 installations.

FIX : Either upgrade to v17, or perform manual conversion : promote the standby, set up `CREATE PUBLICATION` on the source, `CREATE SUBSCRIPTION` on the converted node, manage initial sync via copy_data option.

## AP-11 : Mixing non-decimal integer literals into a v15-targeted query

CAUSE : v16 added non-decimal integer literals (`0x42F`, `0b101`, `0o273`, `1_000_000`). Code targeting v15 borrowed this syntax.

SYMPTOM : `ERROR: syntax error at or near "x42F"` (SQLSTATE 42601). Query rejected at parse time.

FIX : ALWAYS write integer constants in plain decimal for cross-version code. The v16+ alternative syntaxes are convenience-only and offer no semantic features.

## AP-12 : COPY DEFAULT on v15

CAUSE : v16 added `COPY ... WITH (DEFAULT 'token')` to map placeholder tokens in CSV to column DEFAULT expressions. On v15 the same option is unknown.

SYMPTOM : `ERROR: option "default" not recognized` (SQLSTATE 22023). COPY fails.

FIX : Either upgrade to v16+, or pre-process the CSV externally to replace placeholder tokens with literal values that the v15 server accepts.

## AP-13 : pg_use_reserved_connections on v15

CAUSE : v16 added the `pg_use_reserved_connections` predefined role and the corresponding `reserved_connections` GUC.

SYMPTOM : `ERROR: role "pg_use_reserved_connections" does not exist` (SQLSTATE 42704) when running `GRANT pg_use_reserved_connections TO ...`. Or, on v15, `reserved_connections` GUC does not exist.

FIX : v16+ feature ; on v15 the closest equivalent is the long-standing `superuser_reserved_connections` GUC (superuser-only reservation, not delegated).

## AP-14 : Ignoring CREATEROLE rewrite when upgrading to v16

CAUSE : v16 rewrote the semantics of CREATEROLE : a CREATEROLE-only user can no longer create / drop / modify arbitrary roles. ADMIN OPTION on a target role is now required to alter that role's membership.

SYMPTOM : After pg_upgrade to v16, role-management scripts that were previously executed by a non-superuser with only CREATEROLE start failing with `ERROR: must have admin option on role "X"` (SQLSTATE 42501).

FIX : Grant ADMIN OPTION explicitly : `GRANT some_role TO admin_user WITH ADMIN OPTION;`. Or use a superuser for role-management automation. Pre-v16 the loose CREATEROLE semantics were a documented privilege-escalation surface.

## AP-15 : Counting on JSON constructor functions on v15

CAUSE : v16 added the SQL-standard JSON constructor functions (`JSON_ARRAY`, `JSON_OBJECT`, `JSON_ARRAYAGG`, `JSON_OBJECTAGG`) and the `IS JSON` predicate.

SYMPTOM : `ERROR: function json_array(...) does not exist` (SQLSTATE 42883) on v15.

FIX : On v15, use legacy constructors : `jsonb_build_array(...)`, `jsonb_build_object(...)`, `jsonb_agg(...)`, `jsonb_object_agg(...)`. They are functionally equivalent for most cases.

## AP-16 : Using v17 streaming-I/O-dependent expectations on v15+v16

CAUSE : Author benchmarked sequential-scan or VACUUM throughput on v17 and assumed the same numbers apply to v15+v16. v17 added a streaming I/O subsystem that drastically improves these paths.

SYMPTOM : No error, but actual production throughput on v15+v16 is significantly worse than the documented v17 benchmark.

FIX : Re-benchmark on the actual target version. If the v17 numbers are required, plan the upgrade ; do not assume backporting tuning parameters reproduces v17 behaviour.

## SQLSTATE quick reference

| Anti-pattern | SQLSTATE | PostgreSQL error name |
|---|---|---|
| Unknown syntax (parser-stage failure) | 42601 | syntax_error |
| Function does not exist | 42883 | undefined_function |
| Permission denied for schema | 42501 | insufficient_privilege |
| Unrecognized configuration parameter | 22023 / 42704 | invalid_parameter_value / undefined_object |
| Undefined role / GUC name | 42704 | undefined_object |
| Statement / lock timeout | 57014 | query_canceled |

## Sources

- https://www.postgresql.org/docs/release/17.0/
- https://www.postgresql.org/docs/release/16.0/
- https://www.postgresql.org/docs/release/15.0/
- https://www.postgresql.org/docs/17/errcodes-appendix.html
- https://www.postgresql.org/docs/17/sql-merge.html
- https://www.postgresql.org/docs/17/functions-json.html
- https://www.postgresql.org/docs/17/sql-copy.html
- https://www.postgresql.org/docs/17/predefined-roles.html
- https://www.postgresql.org/docs/17/runtime-config-client.html
- CVE-2018-1058 background on public schema lockdown
