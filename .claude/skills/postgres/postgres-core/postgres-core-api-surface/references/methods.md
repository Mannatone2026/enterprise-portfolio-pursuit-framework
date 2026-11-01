# Methods : PostgreSQL API Surface

Full reference of DDL / DML / DCL / TCL grammar, psql meta-commands, and system catalogs for PostgreSQL 15, 16, 17.

## DDL : Data Definition Language

### CREATE statements (most-used)

| Statement | Purpose | Notes |
|-----------|---------|-------|
| `CREATE TABLE` | Define a relation | Supports `IF NOT EXISTS`, `LIKE source INCLUDING ALL`, `INHERITS`, `PARTITION BY` (RANGE / LIST / HASH). |
| `CREATE TABLE ... AS SELECT` | Materialize a query as table | `WITH NO DATA` creates empty schema-only copy. |
| `CREATE INDEX` | B-tree, GIN, GiST, SP-GiST, BRIN, Hash. | `CONCURRENTLY` avoids ACCESS EXCLUSIVE lock ; `INCLUDE (col)` for covering indexes (v11+). |
| `CREATE UNIQUE INDEX` | Enforce uniqueness | Can be partial via `WHERE`. |
| `CREATE VIEW` | Virtual relation | Updatable if simple ; `WITH CHECK OPTION` enforces predicate on INSERT/UPDATE. |
| `CREATE MATERIALIZED VIEW` | Cached query result | Refresh via `REFRESH MATERIALIZED VIEW [CONCURRENTLY]`. |
| `CREATE SCHEMA` | Namespace | Owner via `AUTHORIZATION role`. |
| `CREATE ROLE` / `CREATE USER` | Authentication principal | `USER` = `ROLE ... LOGIN` ; v16 added GRANT options ADMIN / INHERIT / SET. |
| `CREATE FUNCTION` / `CREATE PROCEDURE` | Routines | `LANGUAGE plpgsql / sql / plpython3u` ; procedures support transaction control inside body. |
| `CREATE TRIGGER` | Hook DML / DDL events | `BEFORE` / `AFTER` / `INSTEAD OF` ; `FOR EACH ROW` or `FOR EACH STATEMENT`. |
| `CREATE TYPE` | ENUM, composite, range, base type. | ENUM members are immutable order, append via `ALTER TYPE ... ADD VALUE`. |
| `CREATE DOMAIN` | Constrained type alias | `CHECK` predicate enforced on column. |
| `CREATE EXTENSION` | Load contrib module | `CREATE EXTENSION IF NOT EXISTS pg_stat_statements`. |
| `CREATE PUBLICATION` | Logical replication source | v15+ row filters via `WHERE`, column lists via `(col, ...)`. |
| `CREATE SUBSCRIPTION` | Logical replication sink | Requires `CREATE SUBSCRIPTION` privilege ; v16 added `password_required`, `run_as_owner`. |
| `CREATE POLICY` | Row-Level Security predicate | Requires `ALTER TABLE ... ENABLE ROW LEVEL SECURITY` ; `USING` (read) vs `WITH CHECK` (write). |

### ALTER statements

`ALTER TABLE` is the most-used DDL command. Critical sub-clauses :

| Sub-clause | Lock taken | Rewrites table? |
|------------|-----------|-----------------|
| `ADD COLUMN col type` | ACCESS EXCLUSIVE | No (v11+ if `DEFAULT` is constant or NULL). |
| `ADD COLUMN col type DEFAULT volatile_expr` | ACCESS EXCLUSIVE | Yes (full rewrite). |
| `DROP COLUMN col` | ACCESS EXCLUSIVE | No (logical drop). |
| `ALTER COLUMN col TYPE new_type` | ACCESS EXCLUSIVE | Yes if not implicit cast. |
| `ALTER COLUMN col SET NOT NULL` | ACCESS EXCLUSIVE | Scans (v11+ skips scan if a `CHECK (col IS NOT NULL) NOT VALID` was validated first). |
| `ADD CONSTRAINT ... NOT VALID` | SHARE UPDATE EXCLUSIVE | No. `VALIDATE CONSTRAINT` later. |
| `ATTACH PARTITION` | ACCESS EXCLUSIVE on parent, SHARE UPDATE EXCLUSIVE on partition | No if `CHECK (predicate)` already exists. |
| `DETACH PARTITION ... CONCURRENTLY` | SHARE UPDATE EXCLUSIVE | No (v14+). |
| `SET (fillfactor = N)` | SHARE UPDATE EXCLUSIVE | No until next `VACUUM FULL` / rewrite. |

### DROP statements

| Statement | Cascade behavior |
|-----------|------------------|
| `DROP TABLE` | `CASCADE` removes dependent views, foreign keys, sequences owned by it. `RESTRICT` (default) blocks on any dependent. |
| `DROP INDEX [CONCURRENTLY]` | `CONCURRENTLY` avoids ACCESS EXCLUSIVE lock. |
| `DROP SCHEMA` | `CASCADE` removes all objects in schema. |
| `DROP ROLE` | Blocks if role owns objects ; reassign via `REASSIGN OWNED BY old_role TO new_role`. |
| `DROP EXTENSION` | `CASCADE` removes all objects belonging to the extension. |

### TRUNCATE

```sql
TRUNCATE [TABLE] [ONLY] tab1 [, tab2] [RESTART IDENTITY | CONTINUE IDENTITY] [CASCADE];
```

- Takes ACCESS EXCLUSIVE lock.
- Skips MVCC : empties file immediately, no per-row processing.
- Resets sequences only if `RESTART IDENTITY` is specified.
- Cascades to tables referencing via foreign key when `CASCADE` given ; otherwise raises SQLSTATE `0A000`.

## DML : Data Manipulation Language

### SELECT

```sql
[WITH [RECURSIVE] cte_name AS (query) [, ...]]
SELECT [ALL | DISTINCT [ON (expr)]] select_list
  FROM from_item [, ...]
  [WHERE condition]
  [GROUP BY [ALL | DISTINCT] expr [, ...] [GROUPING SETS | ROLLUP | CUBE]]
  [HAVING condition]
  [WINDOW name AS (window_def)]
  [UNION | INTERSECT | EXCEPT [ALL | DISTINCT] select]
  [ORDER BY expr [ASC | DESC] [NULLS FIRST | NULLS LAST] [, ...]]
  [LIMIT count] [OFFSET start]
  [FOR { UPDATE | NO KEY UPDATE | SHARE | KEY SHARE } [OF table [, ...]] [NOWAIT | SKIP LOCKED]];
```

Key locking modes :
- `FOR UPDATE` : strongest row lock ; blocks any concurrent update or delete.
- `FOR NO KEY UPDATE` : weaker, allows updates on non-key columns concurrently.
- `FOR SHARE` : shared lock ; blocks updates but allows other shared locks.
- `FOR KEY SHARE` : weakest ; only blocks updates to key columns.

### INSERT

```sql
INSERT INTO target [AS alias] [(col_list)]
  { VALUES (expr, ...) [, ...] | query | DEFAULT VALUES }
  [ON CONFLICT [conflict_target] {DO NOTHING | DO UPDATE SET ... [WHERE ...]}]
  [RETURNING * | expr_list];
```

Conflict target forms (one of) :
- `(col, ...)` : column list infers unique index.
- `(col, ...) WHERE predicate` : matches partial unique index with that predicate.
- `ON CONSTRAINT constraint_name` : named NOT DEFERRABLE unique or primary key constraint.

The `EXCLUDED` pseudo-relation references the row proposed for insertion. Existing row referenced by table name or alias.

### UPDATE

```sql
UPDATE [ONLY] target [AS alias]
  SET col = expr [, ...] | (col1, col2) = (expr1, expr2) | (col1, col2) = (SELECT ...)
  [FROM additional_from]
  [WHERE condition]
  [RETURNING * | expr_list];
```

`FROM` clause enables join-update : `UPDATE a SET col = b.col FROM b WHERE a.id = b.id`.

### DELETE

```sql
DELETE FROM [ONLY] target [AS alias]
  [USING additional_from]
  [WHERE condition]
  [RETURNING * | expr_list];
```

`USING` is the DELETE counterpart of `FROM` in UPDATE.

### MERGE (v15+)

```sql
MERGE INTO target USING source ON join_condition
  WHEN MATCHED [AND cond] THEN {UPDATE SET ... | DELETE | DO NOTHING}
  WHEN NOT MATCHED [BY TARGET] [AND cond] THEN {INSERT (cols) VALUES (...) | DO NOTHING}
  WHEN NOT MATCHED BY SOURCE [AND cond] THEN {UPDATE SET ... | DELETE | DO NOTHING}
  [RETURNING * | merge_action(), expr_list];
```

Version annotations :
- v15 : basic MERGE, WHEN MATCHED, WHEN NOT MATCHED (no BY TARGET / BY SOURCE), no RETURNING.
- v16 : adds `WHEN NOT MATCHED BY SOURCE`, `DO NOTHING` action, `RETURNING` clause, `merge_action()` function.
- v17 : adds explicit `BY TARGET` qualifier (clarity), MERGE on updatable views with RULES.

Target restrictions : NEVER allowed on materialized views, foreign tables, or tables with rules.

### TRUNCATE / COPY : see DDL section above and postgres-impl-bulk-loading for COPY.

## DCL : Data Control Language

### GRANT / REVOKE

```sql
GRANT { privilege [, ...] | ALL [PRIVILEGES] }
  ON { [TABLE] tab [, ...]
     | ALL TABLES IN SCHEMA schema [, ...]
     | SEQUENCE seq [, ...]
     | DATABASE db [, ...]
     | SCHEMA schema [, ...]
     | FUNCTION fn (arg_types) [, ...] }
  TO { role [, ...] | PUBLIC }
  [WITH GRANT OPTION];

REVOKE [GRANT OPTION FOR] privilege ON ... FROM role [CASCADE | RESTRICT];
```

Table-level privileges : `SELECT`, `INSERT`, `UPDATE`, `DELETE`, `TRUNCATE`, `REFERENCES`, `TRIGGER`.
Column-level : `GRANT SELECT (col1, col2) ON tab TO role`.
Schema-level : `USAGE` (needed to access objects), `CREATE` (needed to create new objects).

### ALTER DEFAULT PRIVILEGES

```sql
ALTER DEFAULT PRIVILEGES
  [FOR { ROLE target_role [, ...] | USER target_user [, ...] }]
  [IN SCHEMA schema [, ...]]
  GRANT privilege ON {TABLES | SEQUENCES | FUNCTIONS | TYPES | SCHEMAS} TO role;
```

Critical : `GRANT ... ON ALL TABLES` is a snapshot ; `ALTER DEFAULT PRIVILEGES` is forward-looking. Use both for full coverage.

## TCL : Transaction Control Language

| Statement | Effect |
|-----------|--------|
| `BEGIN [TRANSACTION]` / `START TRANSACTION` | Open transaction. Optional `ISOLATION LEVEL { READ COMMITTED | REPEATABLE READ | SERIALIZABLE }`. |
| `COMMIT` | Persist all changes ; release locks. |
| `ROLLBACK` | Discard all changes since BEGIN. |
| `SAVEPOINT name` | Mark a checkpoint inside an open transaction. |
| `ROLLBACK TO SAVEPOINT name` | Discard work after savepoint, KEEP transaction open. |
| `RELEASE SAVEPOINT name` | Forget the savepoint (cannot ROLLBACK TO it). |
| `SET TRANSACTION { ISOLATION LEVEL ... | READ ONLY | DEFERRABLE }` | Configure current transaction. |
| `SET CONSTRAINTS { ALL | name } { DEFERRED | IMMEDIATE }` | Toggle deferrable FK checks. |
| `PREPARE TRANSACTION 'gid'` | Two-phase commit ; out-of-scope per D-019. |

## psql Meta-Commands

| Command | Purpose | Equivalent SQL (approx) |
|---------|---------|-------------------------|
| `\d [pattern]` | Describe one or more objects (columns, indexes, constraints) | Join `pg_class + pg_attribute + pg_constraint + pg_index`. |
| `\dt [S+] [pattern]` | List tables | `SELECT relname FROM pg_class WHERE relkind = 'r'` |
| `\di [S+] [pattern]` | List indexes | `pg_class WHERE relkind = 'i'` |
| `\dv [S+] [pattern]` | List views | `pg_class WHERE relkind = 'v'` |
| `\dm [S+] [pattern]` | List materialized views | `pg_class WHERE relkind = 'm'` |
| `\df [anptwS+] [pattern]` | List functions ; filter by `a`gg / `n`ormal / `p`rocedure / `t`rigger / `w`indow | `pg_proc JOIN pg_namespace` |
| `\dn [S+] [pattern]` | List schemas | `pg_namespace` |
| `\du [S+] [pattern]` | List roles (alias `\dg`) | `pg_authid` (or `pg_roles` for non-superuser) |
| `\dx [+] [pattern]` | List extensions | `pg_extension` |
| `\dp [S] [pattern]` | List privileges on tables / views / sequences | `pg_class.relacl` |
| `\l [+] [pattern]` | List databases | `pg_database` |
| `\dconfig [+] [pattern]` (v15+) | List configuration parameters | `pg_settings` |
| `\drg [S] [pattern]` (v16+) | List role memberships with ADMIN / INHERIT / SET options | `pg_auth_members` |
| `\timing [on|off]` | Toggle client-side query timing | n/a |
| `\watch [seconds]` | Re-run query buffer on interval | n/a |
| `\copy table FROM 'file' [WITH (option ...)]` | Client-side COPY ; runs with psql user's file permissions | n/a |
| `\i filename` | Execute SQL from file | n/a |
| `\e [filename]` | Open editor with current query buffer | n/a |
| `\!` | Run shell command from psql | n/a |
| `\getenv var ENVVAR` (v15+) | Read environment into psql variable | n/a |
| `\bind val1 val2 ...` (v16+) | Set extended-protocol parameters for next `\g` | n/a |
| `\gexec` | Execute previous result as queries | n/a |
| `\set var val` | Set psql variable | n/a |
| `\q` | Quit psql | n/a |

## System Catalog Map (pg_catalog)

### Relations

| Catalog | Holds | Key columns |
|---------|-------|-------------|
| `pg_class` | Tables, indexes, sequences, views, materialized views, composite types, partitioned tables, foreign tables | `oid`, `relname`, `relnamespace`, `relkind` (r/i/S/v/m/c/p/f), `relowner`, `reltuples`, `relpages`, `relhasindex`, `relpersistence`, `relam` |
| `pg_attribute` | Columns of every relation | `attrelid`, `attname`, `attnum`, `atttypid`, `attnotnull`, `atthasdef`, `attidentity`, `attgenerated`, `attisdropped` |
| `pg_index` | Index definitions | `indexrelid`, `indrelid`, `indnatts`, `indisunique`, `indisprimary`, `indisvalid`, `indpred`, `indexprs` |
| `pg_constraint` | Check, foreign-key, unique, primary-key, exclusion, not-null constraints | `conname`, `conrelid`, `contype` (c/f/p/u/t/x), `confrelid`, `conkey`, `confkey` |
| `pg_namespace` | Schemas | `oid`, `nspname`, `nspowner`, `nspacl` |
| `pg_proc` | Functions, procedures, aggregates, windows | `oid`, `proname`, `pronamespace`, `prorettype`, `proargtypes`, `prokind` (f/p/a/w), `prosrc`, `prolang`, `provolatile`, `proparallel` |
| `pg_type` | All types (built-in, composite, ENUM, domain, range, multirange) | `oid`, `typname`, `typtype` (b/c/d/e/p/r/m), `typcategory`, `typndims`, `typrelid` |
| `pg_authid` | Roles with password / login attributes | `oid`, `rolname`, `rolsuper`, `rolinherit`, `rolcreaterole`, `rolcreatedb`, `rolcanlogin`, `rolreplication`, `rolbypassrls`, `rolpassword`, `rolvaliduntil` — superuser only |
| `pg_roles` | Read-only view over `pg_authid` minus password column | Visible to all. |
| `pg_database` | All databases on the cluster | `datname`, `datdba`, `encoding`, `datcollate`, `datctype`, `datallowconn`, `datconnlimit` |
| `pg_tablespace` | Tablespaces | `spcname`, `spcowner`, `spcacl`, `spcoptions` |
| `pg_extension` | Installed extensions | `extname`, `extversion`, `extnamespace`, `extrelocatable` |
| `pg_publication` / `pg_subscription` | Logical replication objects | See postgres-impl-replication. |
| `pg_policy` | RLS policies | `polname`, `polrelid`, `polcmd`, `polroles`, `polqual`, `polwithcheck`. |

### Statistics / live activity (no information_schema equivalent)

| Catalog | Holds | Useful columns |
|---------|-------|---------------|
| `pg_stat_activity` | Per-backend session info | `pid`, `usename`, `state`, `query`, `wait_event_type`, `wait_event`, `backend_xmin`, `xact_start`. |
| `pg_stat_user_tables` | Per-table I/O & vacuum stats | `seq_scan`, `idx_scan`, `n_tup_ins/upd/del`, `n_live_tup`, `n_dead_tup`, `last_autovacuum`, `last_autoanalyze`. |
| `pg_stat_user_indexes` | Per-index usage | `idx_scan`, `idx_tup_read`, `idx_tup_fetch`. |
| `pg_stat_statements` | Per-normalized-query stats (extension) | `query`, `calls`, `total_exec_time`, `mean_exec_time`, `rows`, `shared_blks_hit/read`. |
| `pg_stat_replication` | Streaming replicas | `client_addr`, `state`, `sent_lsn`, `write_lsn`, `flush_lsn`, `replay_lsn`. |
| `pg_settings` | Configuration parameters live | `name`, `setting`, `unit`, `category`, `context`, `source`, `min_val`, `max_val`. |
| `pg_locks` | Granted and waiting locks | `relation`, `mode`, `granted`, `pid`, `transactionid`, `virtualxid`. |
| `pg_stat_io` (v16+) | Backend-type I/O stats | `backend_type`, `object`, `reads`, `read_time`, `writes`, `write_time`. |
| `pg_wait_events` (v17+) | Catalog of all wait event names with description | `type`, `name`, `description`. |

## information_schema vs pg_catalog : when to choose which

| Need | Use |
|------|-----|
| PostgreSQL-specific (extensions, stats, settings, replication) | `pg_catalog` |
| SQL-standard portable inspection (`columns`, `tables`, `views`) | `information_schema` |
| Performance-sensitive introspection (CI scripts, schema diff tools) | `pg_catalog` (5-50x faster) |
| User-facing dashboards or cross-engine apps | `information_schema` |

`information_schema` is implemented as views over `pg_catalog` — anything reachable via `information_schema` is reachable via `pg_catalog`, with extra columns and no view overhead.

## Sources verified

- https://www.postgresql.org/docs/17/sql-merge.html
- https://www.postgresql.org/docs/17/sql-insert.html
- https://www.postgresql.org/docs/17/app-psql.html
- https://www.postgresql.org/docs/17/catalogs.html
- https://www.postgresql.org/docs/17/sql-altertable.html
- https://www.postgresql.org/docs/17/sql-grant.html
- https://www.postgresql.org/docs/17/sql-set-transaction.html
- https://www.postgresql.org/docs/release/17.0/, /16.0/, /15.0/
