---
name: postgres-core-api-surface
description: >
  Use when navigating PostgreSQL's SQL surface, picking the right system catalog, or learning psql meta-commands.
  Prevents grabbing information_schema when pg_catalog is faster, or hunting for system info without knowing the catalog map.
  Covers DDL/DML/DCL/TCL grammar overview, psql meta-commands, pg_catalog vs information_schema decision tree, system catalog map.
  Keywords: pg_catalog, information_schema, psql, meta-commands, system catalogs, DDL, DML, DCL, TCL, MERGE, ON CONFLICT, where is my table info, how to list users, how to inspect schema, what does \d do
license: MIT
compatibility: "Designed for Claude Code. Requires PostgreSQL 15, 16, or 17."
metadata:
  author: OpenAEC-Foundation
  version: "1.0"
---

# postgres-core-api-surface

## Quick Reference :

PostgreSQL's command surface splits into four orthogonal layers and one out-of-band tool channel :

- DDL : `CREATE` / `ALTER` / `DROP` / `TRUNCATE` for schema objects (tables, indexes, views, roles, types, functions, policies, publications, subscriptions).
- DML : `SELECT` / `INSERT` / `UPDATE` / `DELETE` / `MERGE` (v15+) / `COPY` for data.
- DCL : `GRANT` / `REVOKE` / `ALTER DEFAULT PRIVILEGES` for access control.
- TCL : `BEGIN` / `COMMIT` / `ROLLBACK` / `SAVEPOINT` / `RELEASE` / `ROLLBACK TO` / `SET TRANSACTION` for transaction boundaries.
- `psql` meta-commands : backslash commands (`\d`, `\dt`, `\df`, `\du`, `\timing`, `\watch`, `\copy`, `\i`, `\e`, `\!`) drive the database operationally.

When you need to introspect the live database, prefer `pg_catalog` over `information_schema` : `pg_catalog` is faster, version-stable for PostgreSQL features, and exposes PostgreSQL-specific columns (`pg_class.reltuples`, `pg_attribute.attnotnull`). Use `information_schema` only when portability across SQL engines is a hard requirement.

## When To Use This Skill :

ALWAYS use this skill when :
- A task says "introspect schema", "list tables", "find privileges", "inspect indexes" : pick `pg_catalog` or `information_schema` correctly.
- You need to choose between `INSERT ... ON CONFLICT` and `MERGE` (v15+) for upsert.
- You need TCL semantics : nested transactions via `SAVEPOINT`, isolation levels via `SET TRANSACTION`.
- You need a `psql` meta-command equivalent to a SQL query (e.g. `\d table` is shorthand, not magic).

NEVER use this skill for :
- Performance tuning : see postgres-core-mvcc-wal and postgres-impl-explain.
- Index access methods (B-tree vs GIN vs BRIN) : see postgres-syntax-indexing.
- Role / privilege design : see postgres-impl-security and postgres-impl-rls.
- Deep `pl/pgSQL` programming : out-of-scope per D-019.

## Decision Trees :

### Catalog selection : `pg_catalog` vs `information_schema` :

```
Need PostgreSQL-specific info (reltuples, attnotnull, pg_index.indnatts, pg_authid.rolcanlogin)?
  -> pg_catalog (always faster, no view overhead, version-stable per release)

Need portability across PostgreSQL / MySQL / SQL Server / Oracle?
  -> information_schema (SQL standard, slower because it is a view over pg_catalog)

Need extension metadata (pg_extension, pg_proc.proowner)?
  -> pg_catalog (information_schema does not expose extensions)

Need statistics or live activity (pg_stat_user_tables, pg_stat_activity, pg_settings)?
  -> pg_catalog ONLY (information_schema has no equivalent)
```

### Upsert : `INSERT ... ON CONFLICT` vs `MERGE` :

```
Single row or small batch, conflict target is one unique index?
  -> INSERT ... ON CONFLICT (target) DO UPDATE SET col = EXCLUDED.col

Need to atomically INSERT OR UPDATE OR DELETE based on join shape?
  -> MERGE (v15+)

Need full-sync pattern (DELETE rows in target that no longer exist in source)?
  -> MERGE ... WHEN NOT MATCHED BY SOURCE THEN DELETE (v16+, BY TARGET v17+)

Need rows returned with the action taken (INSERT vs UPDATE vs DELETE)?
  -> MERGE ... RETURNING merge_action(), * (v17+)

Target is a materialized view, foreign table, or has rules?
  -> NEITHER : MERGE rejects these; ON CONFLICT requires NOT DEFERRABLE unique constraint.
```

### Transaction boundary tool :

```
Single statement, no rollback needed?
  -> autocommit (no explicit BEGIN)

Multi-statement atomicity, all-or-nothing?
  -> BEGIN; ...; COMMIT;  (or ROLLBACK; on error)

Need partial rollback to a checkpoint inside a transaction?
  -> SAVEPOINT sp_1; ...; ROLLBACK TO SAVEPOINT sp_1; (then RELEASE sp_1 or COMMIT)

Need to isolate from concurrent writes (phantom reads, lost updates)?
  -> BEGIN ISOLATION LEVEL { REPEATABLE READ | SERIALIZABLE };
```

### `psql` for ops vs SQL for code :

```
Interactive inspection during a session?
  -> Backslash command (\dt, \df, \dn, \du, \dx, \l)

Inside an application, script, or CI job?
  -> Plain SQL against pg_catalog (psql meta-commands are NOT portable across drivers)

One-off bulk load from a local file?
  -> \copy (client-side, runs with client's file permissions)

Server-side bulk load from a server-local file?
  -> COPY (server-side, requires pg_read_server_files role or superuser)
```

## Patterns :

### Pattern 1 : Atomic upsert via `INSERT ... ON CONFLICT`

ALWAYS : name a conflict target (column list or `ON CONSTRAINT name`).
NEVER : omit the conflict target ; PostgreSQL requires it to pick the arbiter index.

```sql
INSERT INTO products (sku, name, price)
VALUES ('SKU-001', 'Widget', 9.99)
ON CONFLICT (sku) DO UPDATE
  SET name = EXCLUDED.name,
      price = EXCLUDED.price
  WHERE products.price <> EXCLUDED.price
RETURNING sku, (xmax = 0) AS inserted;
```

WHY : `EXCLUDED` is the row proposed for insertion. The `WHERE` clause skips no-op updates so heap pressure stays low. `xmax = 0` distinguishes INSERT (true) from UPDATE (false) in `RETURNING`.

### Pattern 2 : Full-sync `MERGE` (v17+ with `RETURNING merge_action()`)

ALWAYS : ensure the `ON` clause produces at most one source row per target row.
NEVER : use `MERGE` on materialized views, foreign tables, or rule-defined tables : rejected with error.

```sql
MERGE INTO inventory tgt
USING staging_inventory src
  ON tgt.sku = src.sku
WHEN MATCHED AND tgt.qty <> src.qty THEN
  UPDATE SET qty = src.qty
WHEN NOT MATCHED BY TARGET THEN
  INSERT (sku, qty) VALUES (src.sku, src.qty)
WHEN NOT MATCHED BY SOURCE THEN
  DELETE
RETURNING merge_action(), tgt.sku, tgt.qty;
```

WHY : `merge_action()` returns `'INSERT'`, `'UPDATE'`, or `'DELETE'` per affected row. `WHEN NOT MATCHED BY SOURCE` (v16+) lets you remove rows that disappeared from staging.

### Pattern 3 : Nested transaction via `SAVEPOINT`

ALWAYS : `RELEASE` savepoints you no longer need, otherwise they linger and consume memory.
NEVER : assume a `ROLLBACK TO SAVEPOINT` ends the transaction. It only rewinds to the savepoint; you must still `COMMIT` or `ROLLBACK`.

```sql
BEGIN;
  INSERT INTO orders (customer_id, total) VALUES (42, 100.00);
  SAVEPOINT before_lines;
    INSERT INTO order_lines (order_id, sku, qty) VALUES (currval('orders_id_seq'), 'BAD-SKU', 1);
    -- raises foreign_key_violation (SQLSTATE 23503)
  ROLLBACK TO SAVEPOINT before_lines;
  -- order header is preserved, lines are not
COMMIT;
```

WHY : a single error inside a transaction puts the whole transaction in aborted state. `SAVEPOINT` lets you recover from a sub-step failure without losing earlier work.

### Pattern 4 : Catalog query for table size + index list

ALWAYS : join `pg_class` to `pg_namespace` and filter `nspname` to scope the schema.
NEVER : assume `oid` is the join key for `pg_class.relfilenode` : `relfilenode` changes across `VACUUM FULL` / `REINDEX`.

```sql
SELECT
  n.nspname        AS schema_name,
  c.relname        AS table_name,
  pg_size_pretty(pg_total_relation_size(c.oid)) AS total_size,
  c.reltuples::bigint AS estimated_rows,
  (SELECT count(*) FROM pg_index i WHERE i.indrelid = c.oid) AS index_count
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind = 'r'                       -- ordinary table
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
ORDER BY pg_total_relation_size(c.oid) DESC
LIMIT 20;
```

WHY : `pg_class.relkind` codes are `r` (table), `i` (index), `S` (sequence), `v` (view), `m` (materialized view), `c` (composite type), `p` (partitioned table), `f` (foreign table). `pg_total_relation_size` includes heap + indexes + TOAST.

### Pattern 5 : `psql` introspection shortcuts that match `pg_catalog` queries

ALWAYS : know which catalog query a backslash command represents — it makes scripting easier.
NEVER : embed `psql` meta-commands in application code ; they are an interactive shell feature only.

```
\dt schema.*       -> SELECT relname FROM pg_class JOIN pg_namespace ON ... WHERE relkind = 'r'
\df schema.*       -> SELECT proname FROM pg_proc JOIN pg_namespace ON ...
\du                -> SELECT rolname FROM pg_authid (or pg_roles if not superuser)
\dn+               -> SELECT nspname, nspowner FROM pg_namespace
\dx                -> SELECT extname, extversion FROM pg_extension
\l                 -> SELECT datname, datacl FROM pg_database
\d table_name      -> joins pg_class + pg_attribute + pg_constraint + pg_index
\timing on         -> client-side timing (no server impact)
\watch 2           -> re-run query buffer every 2 seconds
```

### Pattern 6 : DCL : grant table privileges to a role

ALWAYS : grant on schema first (`USAGE`), then on objects.
NEVER : forget that future tables need `ALTER DEFAULT PRIVILEGES` ; existing grants do not propagate.

```sql
GRANT USAGE ON SCHEMA app TO app_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA app TO app_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA app
  GRANT SELECT ON TABLES TO app_reader;
```

WHY : `GRANT ... ON ALL TABLES` is a one-time snapshot. `ALTER DEFAULT PRIVILEGES` makes the grant apply to future tables created in that schema.

## Anti-Patterns :

(Short list, full content in references/anti-patterns.md)

- Querying `information_schema` for everything : 5-50x slower than `pg_catalog`. See references/anti-patterns.md.
- `INSERT ... ON CONFLICT` without conflict target : SQLSTATE `42601` syntax_error.
- `MERGE` on a join that returns multiple source rows per target row : SQLSTATE `21506` triggered_data_change_violation (cardinality violation).
- Embedding `\d` or `\dt` in an application : meta-commands are psql-only and not understood by the wire protocol.
- Forgetting `ALTER DEFAULT PRIVILEGES` after `GRANT ON ALL TABLES` : new tables created later have no grant.
- `ROLLBACK TO SAVEPOINT` then assuming transaction ended : transaction still open ; must `COMMIT` or `ROLLBACK` explicitly.
- Mixing `\copy` (client-side) and `COPY` (server-side) without checking file location : `COPY 'path'` reads from server disk, `\copy` from client disk.
- Using `pg_class.relfilenode` as a stable identifier : changes after `VACUUM FULL` / `REINDEX` / `CLUSTER`. Use `oid` instead.

## Reference Links :

- [references/methods.md](references/methods.md) : Full DDL/DML/DCL/TCL syntax tables, system catalog map (pg_class / pg_attribute / pg_index / pg_constraint / pg_namespace / pg_proc / pg_authid / pg_stat_* / pg_settings), psql meta-command reference.
- [references/examples.md](references/examples.md) : Working code samples for upsert, MERGE (v15/v16/v17 deltas), savepoints, catalog introspection queries, DCL grant matrices, version-annotated.
- [references/anti-patterns.md](references/anti-patterns.md) : Anti-patterns with cause + symptom + fix-pattern + SQLSTATE codes (42601, 23503, 21506, 42501, 0A000).

## See Also :

- postgres-core-mvcc-wal : transaction snapshots, isolation levels, locking — backs TCL semantics.
- postgres-core-version-matrix : v15 / v16 / v17 feature deltas — confirm MERGE feature availability.
- postgres-syntax-dml-upsert : deep dive on `INSERT ... ON CONFLICT` and `MERGE` patterns.
- postgres-impl-security : role design, privilege grants, predefined roles.
- postgres-errors-sqlstate : SQLSTATE code reference for the anti-patterns above.
- Vooronderzoek section : §2 Full SQL Surface, §22 Implications for Masterplan.
- Official docs : https://www.postgresql.org/docs/17/sql-merge.html , https://www.postgresql.org/docs/17/sql-insert.html , https://www.postgresql.org/docs/17/app-psql.html , https://www.postgresql.org/docs/17/catalogs.html
