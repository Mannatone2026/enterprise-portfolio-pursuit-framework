# Examples : PostgreSQL API Surface

Working code samples for DDL, DML (including UPSERT and MERGE), DCL, TCL, psql meta-commands, and system catalog introspection. All examples are version-annotated and verified against PostgreSQL 15, 16, and 17 documentation.

## DDL Examples

### Create a table with constraints and storage parameters

```sql
-- v15+
CREATE TABLE orders (
  id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  customer_id   bigint NOT NULL REFERENCES customers(id) ON DELETE RESTRICT,
  total_cents   integer NOT NULL CHECK (total_cents >= 0),
  status        text NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'paid', 'shipped', 'cancelled')),
  metadata      jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
)
WITH (fillfactor = 90);

CREATE INDEX orders_customer_idx ON orders (customer_id);
CREATE INDEX orders_status_pending_idx ON orders (created_at) WHERE status = 'pending';
CREATE INDEX orders_metadata_gin ON orders USING gin (metadata jsonb_path_ops);
```

### Add a column without rewriting the table (v11+ behavior, stable in v15-17)

```sql
-- Fast : default is a constant, no rewrite
ALTER TABLE orders ADD COLUMN shipped_at timestamptz DEFAULT NULL;

-- Slow : volatile default forces a full table rewrite
ALTER TABLE orders ADD COLUMN tracking_token uuid DEFAULT gen_random_uuid();
```

### Add a NOT NULL constraint without long ACCESS EXCLUSIVE lock

```sql
-- Step 1 : add as NOT VALID (instant, SHARE UPDATE EXCLUSIVE)
ALTER TABLE orders ADD CONSTRAINT orders_total_nn CHECK (total_cents IS NOT NULL) NOT VALID;

-- Step 2 : validate in background (SHARE UPDATE EXCLUSIVE, allows reads/writes)
ALTER TABLE orders VALIDATE CONSTRAINT orders_total_nn;

-- Step 3 : convert to column-level NOT NULL (v18+ will skip scan; v15-17 must scan)
ALTER TABLE orders ALTER COLUMN total_cents SET NOT NULL;
```

## DML Examples

### `INSERT ... ON CONFLICT` : upsert single row

```sql
-- v15+
INSERT INTO settings (key, value)
VALUES ('feature_flag.new_ui', '{"enabled": true}'::jsonb)
ON CONFLICT (key) DO UPDATE
  SET value = EXCLUDED.value,
      updated_at = now()
  WHERE settings.value <> EXCLUDED.value
RETURNING key, value, (xmax = 0) AS was_insert;
```

The `WHERE settings.value <> EXCLUDED.value` skips no-op rewrites. `xmax = 0` is true for INSERT, false for UPDATE.

### `INSERT ... ON CONFLICT` : upsert batch with computed values

```sql
INSERT INTO metric_counters (name, value, last_seen)
SELECT m.name, m.value, m.ts
FROM staging_metrics m
ON CONFLICT (name) DO UPDATE
  SET value = metric_counters.value + EXCLUDED.value,
      last_seen = GREATEST(metric_counters.last_seen, EXCLUDED.last_seen);
```

### `INSERT ... ON CONFLICT` with partial unique index

```sql
-- Index that enforces uniqueness only for active rows
CREATE UNIQUE INDEX users_email_active_uq ON users (email) WHERE deleted_at IS NULL;

-- Upsert respecting the partial index
INSERT INTO users (email, name) VALUES ('a@example.com', 'Alice')
ON CONFLICT (email) WHERE deleted_at IS NULL DO UPDATE
  SET name = EXCLUDED.name
RETURNING id, email;
```

### `INSERT ... ON CONFLICT ON CONSTRAINT`

```sql
INSERT INTO products (sku, name) VALUES ('SKU-X', 'Widget')
ON CONFLICT ON CONSTRAINT products_pkey DO NOTHING;
```

Use this form when the constraint is named and you want to be explicit. Required when there are multiple unique indexes on the same column set.

### `MERGE` v15 : basic two-way merge

```sql
-- v15 : no RETURNING, no merge_action(), no BY SOURCE
MERGE INTO inventory tgt
USING staging_inventory src
  ON tgt.sku = src.sku
WHEN MATCHED THEN
  UPDATE SET qty = src.qty,
             updated_at = now()
WHEN NOT MATCHED THEN
  INSERT (sku, qty, created_at) VALUES (src.sku, src.qty, now());
```

### `MERGE` v16 : adds `WHEN NOT MATCHED BY SOURCE`, `DO NOTHING`, `RETURNING`

```sql
-- v16+
MERGE INTO inventory tgt
USING staging_inventory src
  ON tgt.sku = src.sku
WHEN MATCHED AND tgt.qty <> src.qty THEN
  UPDATE SET qty = src.qty
WHEN MATCHED THEN
  DO NOTHING                       -- v16+ : skip identical rows explicitly
WHEN NOT MATCHED THEN
  INSERT (sku, qty) VALUES (src.sku, src.qty)
WHEN NOT MATCHED BY SOURCE THEN    -- v16+ : full-sync delete
  DELETE
RETURNING merge_action(), tgt.sku, tgt.qty;
```

### `MERGE` v17 : explicit `BY TARGET`, on updatable views with rules

```sql
-- v17+
MERGE INTO inventory_view tgt
USING staging_inventory src
  ON tgt.sku = src.sku
WHEN MATCHED THEN UPDATE SET qty = src.qty
WHEN NOT MATCHED BY TARGET THEN INSERT (sku, qty) VALUES (src.sku, src.qty)
WHEN NOT MATCHED BY SOURCE THEN DELETE
RETURNING merge_action(), *;
```

### Data-modifying CTE : archive-and-delete in one statement

```sql
WITH archived AS (
  DELETE FROM events
  WHERE created_at < now() - interval '90 days'
  RETURNING *
)
INSERT INTO events_archive
SELECT * FROM archived;
```

Single-statement atomicity : if either part fails, neither commits.

### `UPDATE ... FROM` : join-update

```sql
UPDATE products p
SET price_cents = pn.new_price_cents
FROM staging_new_prices pn
WHERE p.sku = pn.sku
RETURNING p.sku, p.price_cents;
```

### `DELETE ... USING` : join-delete

```sql
DELETE FROM sessions s
USING users u
WHERE s.user_id = u.id AND u.deleted_at IS NOT NULL;
```

## DCL Examples

### Grant a read-only role

```sql
-- Step 1 : schema usage
GRANT USAGE ON SCHEMA app TO app_reader;

-- Step 2 : current tables
GRANT SELECT ON ALL TABLES IN SCHEMA app TO app_reader;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA app TO app_reader;

-- Step 3 : future tables
ALTER DEFAULT PRIVILEGES IN SCHEMA app
  GRANT SELECT ON TABLES TO app_reader;

ALTER DEFAULT PRIVILEGES IN SCHEMA app
  GRANT SELECT ON SEQUENCES TO app_reader;
```

### Column-level grants

```sql
GRANT SELECT (id, email, name), UPDATE (email, name) ON users TO support_role;
REVOKE SELECT (password_hash) ON users FROM support_role;
```

Column-level grants are stored in `pg_attribute.attacl` (NULL means inherit from `pg_class.relacl`).

### v16 : ADMIN, INHERIT, SET options on GRANT role

```sql
-- v16+
GRANT app_admin TO alice WITH ADMIN OPTION;          -- alice can grant app_admin to others
GRANT app_admin TO bob WITH INHERIT FALSE;           -- bob has the role but does not auto-inherit privileges
GRANT app_admin TO carol WITH SET FALSE;             -- carol cannot SET ROLE to app_admin
```

Inspect via `\drg` (v16+) or `SELECT * FROM pg_auth_members`.

## TCL Examples

### Plain transaction with rollback on error

```sql
BEGIN;
  UPDATE accounts SET balance = balance - 100 WHERE id = 1;
  UPDATE accounts SET balance = balance + 100 WHERE id = 2;
  -- If either UPDATE fails, transaction enters aborted state.
  -- Application must issue ROLLBACK explicitly.
COMMIT;
```

### Savepoint : recover from sub-step error

```sql
BEGIN;
  INSERT INTO orders (customer_id, total_cents) VALUES (42, 100) RETURNING id \gset
  SAVEPOINT before_lines;
    INSERT INTO order_lines (order_id, sku, qty) VALUES (:id, 'BAD-SKU', 1);
    -- raises foreign_key_violation SQLSTATE 23503
  ROLLBACK TO SAVEPOINT before_lines;
  INSERT INTO order_lines (order_id, sku, qty) VALUES (:id, 'GOOD-SKU', 1);
COMMIT;
```

### Isolation level for read-consistent reporting

```sql
BEGIN ISOLATION LEVEL REPEATABLE READ;
  SELECT count(*) FROM orders WHERE status = 'paid';
  -- ... more queries that all see the same snapshot ...
COMMIT;
```

### Serializable transaction with retry pattern

```sql
-- Application pseudocode
loop:
  BEGIN ISOLATION LEVEL SERIALIZABLE;
    -- ... business logic ...
  COMMIT;
  -- if SQLSTATE 40001 (serialization_failure), retry the whole transaction
```

`40001` is the only SQLSTATE that always means "safe to retry verbatim".

### Deferrable constraint check at commit

```sql
CREATE TABLE invoice_lines (
  invoice_id bigint NOT NULL,
  line_no    int    NOT NULL,
  PRIMARY KEY (invoice_id, line_no)
    DEFERRABLE INITIALLY IMMEDIATE
);

BEGIN;
  SET CONSTRAINTS ALL DEFERRED;
  -- temporarily reorder line numbers without violating uniqueness mid-transaction
  UPDATE invoice_lines SET line_no = -line_no WHERE invoice_id = 1;
  UPDATE invoice_lines SET line_no = line_no + 100 WHERE invoice_id = 1 AND line_no < 0;
COMMIT;
```

## psql Meta-Command Examples

### Inspect a table

```
\d public.orders
                                       Table "public.orders"
   Column     |           Type           | Collation | Nullable |          Default
--------------+--------------------------+-----------+----------+----------------------------
 id           | bigint                   |           | not null | generated always as identity
 customer_id  | bigint                   |           | not null |
 total_cents  | integer                  |           | not null |
 status       | text                     |           | not null | 'pending'::text
 metadata     | jsonb                    |           | not null | '{}'::jsonb
 created_at   | timestamp with time zone |           | not null | now()
Indexes :
    "orders_pkey" PRIMARY KEY, btree (id)
    "orders_customer_idx" btree (customer_id)
    "orders_status_pending_idx" btree (created_at) WHERE status = 'pending'
    "orders_metadata_gin" gin (metadata jsonb_path_ops)
Check constraints :
    "orders_status_check" CHECK (status = ANY (ARRAY['pending', 'paid', 'shipped', 'cancelled']))
    "orders_total_cents_check" CHECK (total_cents >= 0)
Foreign-key constraints :
    "orders_customer_id_fkey" FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE RESTRICT
```

### List schemas and their owners

```
\dn+
```

### List installed extensions

```
\dx
                         List of installed extensions
       Name        | Version |   Schema   |        Description
-------------------+---------+------------+----------------------------
 pg_stat_statements| 1.10    | public     | track planning and execution statistics of all SQL statements executed
 plpgsql           | 1.0     | pg_catalog | PL/pgSQL procedural language
```

### Monitor a query every 5 seconds (`\watch`)

```
SELECT pid, state, wait_event_type, wait_event, query
FROM pg_stat_activity
WHERE state <> 'idle'
ORDER BY xact_start;
\watch 5
```

### Bulk load from a CSV on the client

```
\copy orders FROM '/local/path/orders.csv' WITH (FORMAT csv, HEADER true)
```

The `\copy` runs on the psql client side. The server-side equivalent `COPY orders FROM '/path' ...` requires the file to be on the server filesystem and the user to have `pg_read_server_files` membership or be a superuser.

## System Catalog Introspection Examples

### List all tables in a schema with sizes

```sql
SELECT
  c.relname        AS table_name,
  pg_size_pretty(pg_table_size(c.oid))        AS heap_size,
  pg_size_pretty(pg_indexes_size(c.oid))      AS index_size,
  pg_size_pretty(pg_total_relation_size(c.oid)) AS total_size,
  c.reltuples::bigint AS estimated_rows
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind = 'r'
  AND n.nspname = 'app'
ORDER BY pg_total_relation_size(c.oid) DESC;
```

### Find unused indexes

```sql
SELECT
  s.schemaname,
  s.relname,
  s.indexrelname,
  s.idx_scan,
  pg_size_pretty(pg_relation_size(s.indexrelid)) AS index_size
FROM pg_stat_user_indexes s
JOIN pg_index i ON i.indexrelid = s.indexrelid
WHERE s.idx_scan = 0
  AND NOT i.indisunique
  AND NOT i.indisprimary
ORDER BY pg_relation_size(s.indexrelid) DESC;
```

### List privileges on a table

```sql
SELECT grantee, privilege_type, is_grantable
FROM information_schema.table_privileges
WHERE table_schema = 'app' AND table_name = 'orders';
```

Note : `information_schema` is fine here because the question is portable ("who can do what on this table"). For PostgreSQL-specific introspection of column-level grants, query `pg_attribute.attacl` directly.

### Show all foreign keys in a schema (`pg_catalog`-native)

```sql
SELECT
  con.conname        AS constraint_name,
  cl.relname         AS table_name,
  array_agg(att.attname ORDER BY u.attposition) AS columns,
  cl_ref.relname     AS references_table,
  array_agg(att_ref.attname ORDER BY u.attposition) AS references_columns,
  con.confupdtype    AS on_update,
  con.confdeltype    AS on_delete
FROM pg_constraint con
JOIN pg_class cl       ON cl.oid = con.conrelid
JOIN pg_class cl_ref   ON cl_ref.oid = con.confrelid
JOIN pg_namespace ns   ON ns.oid = cl.relnamespace
JOIN unnest(con.conkey)  WITH ORDINALITY AS u(attnum, attposition) ON true
JOIN pg_attribute att     ON att.attrelid = cl.oid AND att.attnum = u.attnum
JOIN unnest(con.confkey) WITH ORDINALITY AS uf(attnum, attposition) ON uf.attposition = u.attposition
JOIN pg_attribute att_ref ON att_ref.attrelid = cl_ref.oid AND att_ref.attnum = uf.attnum
WHERE con.contype = 'f'
  AND ns.nspname = 'app'
GROUP BY con.conname, cl.relname, cl_ref.relname, con.confupdtype, con.confdeltype;
```

`information_schema.referential_constraints` exists but is significantly slower because it is a multi-join view.

### Inspect current server settings

```sql
SELECT name, setting, unit, source, context
FROM pg_settings
WHERE name IN ('shared_buffers', 'work_mem', 'maintenance_work_mem', 'effective_cache_size', 'max_wal_size')
ORDER BY name;
```

Or in psql : `\dconfig shared_buffers` (v15+).

### Find blocking sessions

```sql
SELECT
  blocked.pid           AS blocked_pid,
  blocked.query         AS blocked_query,
  blocker.pid           AS blocker_pid,
  blocker.query         AS blocker_query,
  age(now(), blocked.xact_start) AS blocked_age
FROM pg_stat_activity blocked
JOIN pg_stat_activity blocker
  ON blocker.pid = ANY (pg_blocking_pids(blocked.pid))
WHERE blocked.wait_event_type = 'Lock'
ORDER BY blocked_age DESC;
```

## Sources verified

- https://www.postgresql.org/docs/17/sql-insert.html
- https://www.postgresql.org/docs/17/sql-merge.html
- https://www.postgresql.org/docs/17/sql-altertable.html
- https://www.postgresql.org/docs/17/sql-grant.html
- https://www.postgresql.org/docs/17/sql-set-transaction.html
- https://www.postgresql.org/docs/17/app-psql.html
- https://www.postgresql.org/docs/17/catalogs.html
- https://www.postgresql.org/docs/release/16.0/
- https://www.postgresql.org/docs/release/17.0/
