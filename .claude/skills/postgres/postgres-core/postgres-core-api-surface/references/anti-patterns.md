# Anti-Patterns : PostgreSQL API Surface

Common mistakes when navigating PostgreSQL's SQL surface, with cause, symptom, fix pattern, and SQLSTATE codes where applicable.

## 1. Querying `information_schema` for everything

**Cause :** Habit from MySQL or SQL Server, where `information_schema` is the only inspection surface.

**Symptom :** Schema-diff and CI scripts that take 10-60 seconds on databases that should answer in milliseconds. Worst-case : queries that scan every column-grant ACL on every table in the cluster.

**Why it happens :** `information_schema` is a portable SQL-standard wrapper implemented as views over `pg_catalog`. Every query does extra work to (a) format catalog data into standard column names, (b) apply permission filtering on every row, (c) join across many catalog tables.

**Fix pattern :** Query `pg_catalog` directly. Replace :

```sql
-- Slow
SELECT column_name, data_type
FROM information_schema.columns
WHERE table_schema = 'app' AND table_name = 'orders';

-- Fast
SELECT a.attname, format_type(a.atttypid, a.atttypmod)
FROM pg_attribute a
JOIN pg_class c     ON c.oid = a.attrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'app' AND c.relname = 'orders'
  AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
```

**SQLSTATE :** none ; this is a performance anti-pattern, not an error.

**Exception :** Use `information_schema` when the same code must run on multiple SQL engines.

## 2. `INSERT ... ON CONFLICT` without conflict target

**Cause :** Treating `ON CONFLICT` like MySQL's `INSERT IGNORE`.

**Symptom :**
```
ERROR:  ON CONFLICT DO UPDATE requires inference specification or constraint name
SQLSTATE : 42601 (syntax_error)
```

**Fix pattern :** Provide a column list, partial-index predicate, or explicit constraint name.

```sql
-- Wrong : no arbiter
INSERT INTO settings (key, value) VALUES ('k', '{}'::jsonb)
ON CONFLICT DO UPDATE SET value = EXCLUDED.value;

-- Right : column list infers arbiter unique index
INSERT INTO settings (key, value) VALUES ('k', '{}'::jsonb)
ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;
```

**Note :** `ON CONFLICT DO NOTHING` without a target IS allowed and means "any unique violation skips the row" but `DO UPDATE` always needs an explicit arbiter.

## 3. `MERGE` with a join that matches multiple source rows per target row

**Cause :** Source query has duplicates, or the `ON` condition is too loose.

**Symptom :**
```
ERROR:  MERGE command cannot affect row a second time
HINT:   Ensure that not more than one source row matches any one target row.
SQLSTATE : 21506 (triggered_data_change_violation, also referenced as cardinality violation)
```

**Fix pattern :** Pre-aggregate the source so each target row joins to at most one source row.

```sql
-- Wrong : staging has duplicates per sku
MERGE INTO inventory tgt
USING staging_inventory src ON tgt.sku = src.sku
WHEN MATCHED THEN UPDATE SET qty = src.qty;

-- Right : aggregate first
MERGE INTO inventory tgt
USING (
  SELECT sku, sum(qty) AS qty
  FROM staging_inventory
  GROUP BY sku
) src ON tgt.sku = src.sku
WHEN MATCHED THEN UPDATE SET qty = src.qty;
```

## 4. Foreign-key violation inside a transaction without savepoint recovery

**Cause :** A single error in a transaction puts the whole transaction in aborted state ; subsequent statements fail with `25P02` until rollback.

**Symptom :**
```
ERROR:  insert or update on table "order_lines" violates foreign key constraint "order_lines_order_id_fkey"
SQLSTATE : 23503 (foreign_key_violation)

-- followed by
ERROR:  current transaction is aborted, commands ignored until end of transaction block
SQLSTATE : 25P02 (in_failed_sql_transaction)
```

**Fix pattern :** Wrap the risky statement in a savepoint.

```sql
BEGIN;
  INSERT INTO orders (customer_id, total_cents) VALUES (42, 100);
  SAVEPOINT lines_sp;
    INSERT INTO order_lines (order_id, sku, qty) VALUES (currval('orders_id_seq'), 'BAD-SKU', 1);
    -- raises 23503
  ROLLBACK TO SAVEPOINT lines_sp;   -- recovers ; transaction still alive
  -- handle error or insert good data
COMMIT;
```

## 5. Embedding `\d` or `\dt` in application code

**Cause :** Confusing `psql` shell shortcuts with SQL.

**Symptom :**
```
ERROR:  syntax error at or near "\"
SQLSTATE : 42601 (syntax_error)
```

**Fix pattern :** Translate the meta-command to its catalog query. `\dt schema.*` is :

```sql
SELECT c.relname
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind = 'r' AND n.nspname = 'schema';
```

Run psql with `-E` (`--echo-hidden`) to see the catalog query for any meta-command.

## 6. `GRANT ... ON ALL TABLES` without `ALTER DEFAULT PRIVILEGES`

**Cause :** Believing that `GRANT ON ALL TABLES IN SCHEMA` is a forward-looking rule.

**Symptom :** New tables created later are not visible to the role. Reads fail with :
```
ERROR:  permission denied for table new_table
SQLSTATE : 42501 (insufficient_privilege)
```

**Fix pattern :** Combine both forms.

```sql
GRANT SELECT ON ALL TABLES IN SCHEMA app TO app_reader;   -- existing tables (snapshot)
ALTER DEFAULT PRIVILEGES IN SCHEMA app
  GRANT SELECT ON TABLES TO app_reader;                   -- future tables (rule)
```

Default privileges are stored in `pg_default_acl`. They apply per-grantor : if `bob` creates a table in the schema, only `ALTER DEFAULT PRIVILEGES FOR ROLE bob` will auto-grant on it.

## 7. `ROLLBACK TO SAVEPOINT` without `COMMIT` or `ROLLBACK`

**Cause :** Misreading `ROLLBACK TO SAVEPOINT` as "end the transaction at this point".

**Symptom :** Transaction stays open. Connection holds locks and `pg_stat_activity.state` shows `idle in transaction` indefinitely. Other sessions block.

**Fix pattern :** Always finish the transaction after a savepoint rollback.

```sql
BEGIN;
  SAVEPOINT sp;
    -- risky work
  ROLLBACK TO SAVEPOINT sp;
COMMIT;        -- or ROLLBACK ; the transaction must end explicitly
```

## 8. Using `pg_class.relfilenode` as a stable table identifier

**Cause :** Assuming `relfilenode` is the persistent on-disk identity.

**Symptom :** Reports or monitoring break after `VACUUM FULL`, `REINDEX`, `CLUSTER`, or `ALTER TABLE ... SET TABLESPACE`. The `relfilenode` value changes ; the `oid` stays.

**Fix pattern :** Join on `pg_class.oid`, not `relfilenode`.

```sql
SELECT c.relname, c.reltuples
FROM pg_class c
WHERE c.oid = 'app.orders'::regclass;
```

`regclass` cast is the canonical way to resolve `schema.name` to `oid`.

## 9. Mixing `COPY` and `\copy` without checking file location

**Cause :** Forgetting that `COPY ... FROM 'path'` is server-side, while `\copy ... FROM 'path'` is client-side.

**Symptom :**
```
ERROR:  could not open file "/home/freek/data.csv" for reading: No such file or directory
SQLSTATE : 58P01 (undefined_file)
```

…even though the file exists on the client.

**Fix pattern :**

```
-- File on client machine
\copy orders FROM '/home/freek/data.csv' WITH (FORMAT csv, HEADER true);

-- File on server machine (requires pg_read_server_files membership or superuser)
COPY orders FROM '/var/lib/postgresql/imports/orders.csv' WITH (FORMAT csv, HEADER true);
```

## 10. `MERGE` on a materialized view or foreign table

**Cause :** Assuming MERGE supports any target that supports DML.

**Symptom :**
```
ERROR:  cannot execute MERGE on relation "mv_orders"
DETAIL: This operation is not supported for materialized views.
SQLSTATE : 0A000 (feature_not_supported)
```

**Fix pattern :** Refresh the materialized view from a regular table that you can MERGE into, or use `REFRESH MATERIALIZED VIEW CONCURRENTLY`. For foreign tables, perform DML directly against the remote and refresh local state.

## 11. Creating a function with `LANGUAGE sql` and expecting transaction control

**Cause :** Believing every routine can run `BEGIN` / `COMMIT`.

**Symptom :** Function created but `COMMIT` inside body raises :
```
ERROR:  invalid transaction termination
SQLSTATE : 2D000 (invalid_transaction_termination)
```

**Fix pattern :** Use `CREATE PROCEDURE` (not function) and call via `CALL`. Procedures support `COMMIT` and `ROLLBACK` if no outer transaction is open and the procedure is called at the top level.

```sql
CREATE PROCEDURE batch_archive() AS $$
BEGIN
  LOOP
    DELETE FROM events
    WHERE ctid IN (SELECT ctid FROM events WHERE created_at < now() - interval '90 days' LIMIT 10000);
    EXIT WHEN NOT FOUND;
    COMMIT;
  END LOOP;
END;
$$ LANGUAGE plpgsql;

CALL batch_archive();
```

## 12. Querying `pg_authid` as a non-superuser

**Cause :** Looking for `rolname` + `rolpassword` in `pg_authid`.

**Symptom :**
```
ERROR:  permission denied for table pg_authid
SQLSTATE : 42501 (insufficient_privilege)
```

**Fix pattern :** Use `pg_roles` (a view that omits the password column) for non-superuser introspection.

```sql
SELECT rolname, rolcanlogin, rolsuper, rolcreatedb FROM pg_roles;
```

## 13. Adding a `NOT NULL` column with a volatile default in production

**Cause :** `ALTER TABLE t ADD COLUMN c uuid NOT NULL DEFAULT gen_random_uuid()` rewrites the whole table while holding `ACCESS EXCLUSIVE`.

**Symptom :** Lock waits, application timeouts, multi-minute outage on large tables. `pg_stat_activity.wait_event = 'relation'`.

**Fix pattern :** Add nullable, backfill, then enforce NOT NULL.

```sql
ALTER TABLE t ADD COLUMN c uuid;                          -- fast, no rewrite
UPDATE t SET c = gen_random_uuid() WHERE c IS NULL;       -- batched, throttled
ALTER TABLE t ADD CONSTRAINT t_c_nn CHECK (c IS NOT NULL) NOT VALID;
ALTER TABLE t VALIDATE CONSTRAINT t_c_nn;                 -- background scan
ALTER TABLE t ALTER COLUMN c SET NOT NULL;                -- promote to column-level
```

## 14. Using `serial` instead of `GENERATED AS IDENTITY`

**Cause :** Legacy habit. `serial` creates an implicit sequence but does not transfer USAGE on it during `GRANT`.

**Symptom :** After granting INSERT on the table, application fails on insert :
```
ERROR:  permission denied for sequence orders_id_seq
SQLSTATE : 42501 (insufficient_privilege)
```

**Fix pattern :** Use `GENERATED ALWAYS AS IDENTITY` (since v10). Privileges follow the table.

```sql
CREATE TABLE orders (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, ...);
```

## 15. Treating `pg_stat_statements` as always-on

**Cause :** Assuming `pg_stat_statements` is loaded by default.

**Symptom :** `SELECT * FROM pg_stat_statements` raises :
```
ERROR:  relation "pg_stat_statements" does not exist
SQLSTATE : 42P01 (undefined_table)
```

**Fix pattern :** Confirm extension is in `shared_preload_libraries` and created in the target database.

```sql
SHOW shared_preload_libraries;   -- must contain pg_stat_statements
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
```

Restart required if `shared_preload_libraries` is changed.

## SQLSTATE Quick Reference

| Code | Class | Meaning | Anti-pattern in this doc |
|------|-------|---------|---------------------------|
| `0A000` | Feature Not Supported | Action not allowed on this object kind | #10 (MERGE on matview) |
| `21506` | Triggered Data Change Violation | Cardinality violation in MERGE | #3 |
| `23503` | Foreign Key Violation | Referenced row not found | #4 |
| `25P02` | In Failed SQL Transaction | Statement after error without rollback | #4 |
| `2D000` | Invalid Transaction Termination | COMMIT/ROLLBACK in wrong context | #11 |
| `42501` | Insufficient Privilege | Missing GRANT or default-privilege gap | #6, #12, #14 |
| `42601` | Syntax Error | Missing ON CONFLICT target or backslash command in SQL | #2, #5 |
| `42P01` | Undefined Table | Missing extension or relation | #15 |
| `58P01` | Undefined File | Wrong filesystem side for COPY | #9 |

Full SQLSTATE list : https://www.postgresql.org/docs/17/errcodes-appendix.html

## Sources verified

- https://www.postgresql.org/docs/17/sql-insert.html
- https://www.postgresql.org/docs/17/sql-merge.html
- https://www.postgresql.org/docs/17/errcodes-appendix.html
- https://www.postgresql.org/docs/17/sql-altertable.html
- https://www.postgresql.org/docs/17/sql-grant.html
- https://www.postgresql.org/docs/17/sql-copy.html
- https://www.postgresql.org/docs/17/catalogs.html
- https://www.postgresql.org/docs/17/pgstatstatements.html
