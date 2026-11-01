# postgres-core-version-matrix : examples

Working code samples. Each example is annotated with the minimum PostgreSQL version on which it runs. All snippets verified against the v15.0 / v16.0 / v17.0 release notes and the v17 reference manual.

## 1. Version detection

### 1.1 Boolean check in DO block (works v15+v16+v17)

```sql
DO $$
DECLARE
  v_num int := current_setting('server_version_num')::int;
BEGIN
  RAISE NOTICE 'server_version_num = %', v_num;
  IF v_num >= 170000 THEN
    RAISE NOTICE 'v17 features available';
  ELSIF v_num >= 160000 THEN
    RAISE NOTICE 'v16 baseline';
  ELSE
    RAISE NOTICE 'v15 baseline';
  END IF;
END$$;
```

### 1.2 Conditional DDL based on version (works v15+v16+v17)

```sql
DO $$
BEGIN
  IF current_setting('server_version_num')::int >= 170000 THEN
    EXECUTE 'ALTER SYSTEM SET transaction_timeout = ''60s''';
  END IF;
END$$;
```

## 2. UPSERT patterns

### 2.1 Portable UPSERT with RETURNING (works v15+v16+v17)

```sql
INSERT INTO inventory (sku, qty)
VALUES ('A-100', 5)
ON CONFLICT (sku) DO UPDATE
  SET qty = inventory.qty + EXCLUDED.qty
RETURNING sku, qty, (xmax = 0) AS was_insert;
```

### 2.2 MERGE without RETURNING (works v15+v16+v17)

```sql
MERGE INTO inventory AS t
USING (VALUES ('A-100', 5), ('A-101', 2)) AS s(sku, qty)
ON t.sku = s.sku
WHEN MATCHED THEN
  UPDATE SET qty = t.qty + s.qty
WHEN NOT MATCHED THEN
  INSERT (sku, qty) VALUES (s.sku, s.qty);
```

### 2.3 MERGE with RETURNING (v17+ ONLY)

```sql
-- v17+ ONLY. Will fail with syntax error on v15+v16.
MERGE INTO inventory AS t
USING (VALUES ('A-100', 5), ('A-101', 2)) AS s(sku, qty)
ON t.sku = s.sku
WHEN MATCHED THEN
  UPDATE SET qty = t.qty + s.qty
WHEN NOT MATCHED THEN
  INSERT (sku, qty) VALUES (s.sku, s.qty)
RETURNING merge_action(), t.sku, t.qty;
```

### 2.4 MERGE WHEN NOT MATCHED BY SOURCE (v17+ ONLY)

```sql
-- v17+ ONLY. Full-sync : delete target rows not present in source.
MERGE INTO inventory AS t
USING current_catalog AS s
ON t.sku = s.sku
WHEN MATCHED THEN UPDATE SET qty = s.qty
WHEN NOT MATCHED THEN INSERT (sku, qty) VALUES (s.sku, s.qty)
WHEN NOT MATCHED BY SOURCE THEN DELETE;
```

## 3. JSON projection

### 3.1 Portable JSON projection (works v15+v16+v17)

```sql
-- Project a JSON array into rows using jsonb_to_recordset
SELECT id, name
FROM jsonb_to_recordset('[
  {"id": 1, "name": "Alice"},
  {"id": 2, "name": "Bob"}
]'::jsonb) AS x(id int, name text);
```

### 3.2 JSON_TABLE (v17+ ONLY)

```sql
-- v17+ ONLY. Returns ERROR: function json_table does not exist on v15+v16.
SELECT *
FROM JSON_TABLE(
  '{"items":[{"id":1,"name":"Alice"},{"id":2,"name":"Bob"}]}'::jsonb,
  '$.items[*]'
  COLUMNS (
    id   int  PATH '$.id',
    name text PATH '$.name'
  )
) AS jt;
```

### 3.3 IS JSON predicate (v16+)

```sql
-- v16+. Validates whether a text or jsonb value is a JSON document.
SELECT col, col IS JSON OBJECT AS is_obj
FROM (VALUES ('{"a":1}'), ('not json')) v(col);
```

## 4. Timeouts

### 4.1 statement_timeout + idle_in_transaction (works v15+v16+v17)

```sql
SET statement_timeout = '10s';
SET idle_in_transaction_session_timeout = '30s';

BEGIN;
  SELECT pg_sleep(20);  -- fails at 10s with SQLSTATE 57014
COMMIT;
```

### 4.2 transaction_timeout (v17+ ONLY)

```sql
-- v17+ ONLY. SET transaction_timeout fails with SQLSTATE 22023 on v15+v16.
SET transaction_timeout = '30s';

BEGIN;
  -- if total wall-clock time exceeds 30s, the transaction is cancelled.
  SELECT pg_sleep(15);
  SELECT pg_sleep(20);  -- pushes total over 30s, cancellation triggers
COMMIT;
```

### 4.3 idle_session_timeout (works v15+v16+v17 ; introduced in v14)

```sql
-- terminate any idle session after 5 minutes
SET idle_session_timeout = '5min';
```

## 5. security_invoker views (v15+)

```sql
-- v15+. Defaults to security_definer (executes as view owner) if omitted.
CREATE VIEW orders_v WITH (security_invoker = true) AS
  SELECT id, customer_id, total FROM orders WHERE deleted_at IS NULL;

-- toggle on an existing view
ALTER VIEW orders_v SET (security_invoker = true);
```

## 6. public schema lockdown (v15+ new databases)

```sql
-- v15+. New database created with default permissions :
--   USAGE granted to PUBLIC, CREATE NOT granted to PUBLIC.

-- to allow an app role to create objects in public :
GRANT CREATE ON SCHEMA public TO app_user;

-- preferred : isolate per-app schemas
CREATE SCHEMA app AUTHORIZATION app_user;
ALTER ROLE app_user SET search_path = app, public;
```

## 7. COPY DEFAULT (v16+)

```sql
-- v16+. Tokens matching '\D' pull the column DEFAULT.
CREATE TABLE orders (
  id          int PRIMARY KEY,
  status      text DEFAULT 'pending',
  created_at  timestamptz DEFAULT now()
);

COPY orders (id, status, created_at)
FROM STDIN WITH (FORMAT csv, DEFAULT '\D');
1,paid,\D
2,\D,\D
3,refunded,2025-01-03 12:00:00+00
\.
```

## 8. Logical replication from standby (v16+)

```sql
-- v16+. On the primary :
SELECT pg_log_standby_snapshot();

-- v16+. On the standby :
SELECT pg_create_logical_replication_slot('mig_slot', 'pgoutput');
CREATE PUBLICATION mig_pub FOR ALL TABLES;
-- (subscriber connects to standby's connection string)
```

## 9. ORIGIN filter for bidirectional logical replication (v16+)

```sql
-- v16+. Subscriber ignores changes originating from any publisher,
-- preventing replication loops in active-active topologies.
CREATE SUBSCRIPTION sub_b
  CONNECTION 'host=node-a dbname=app user=replicator'
  PUBLICATION pub_a
  WITH (origin = none);
```

## 10. Incremental backup (v17+)

```bash
# v17+ ONLY. Configure on primary :
#   summarize_wal = on
#   wal_summary_keep_time = '7d'

# Baseline
pg_basebackup -D /backup/full -Fp -X stream -c fast

# Incremental against baseline
pg_basebackup --incremental=/backup/full/backup_manifest -D /backup/incr1 -Fp

# Combine for restore
pg_combinebackup -o /restore /backup/full /backup/incr1

# Verify
pg_verifybackup /restore
```

## 11. pg_createsubscriber (v17+)

```bash
# v17+ ONLY. Operates on a physical standby ; converts to logical subscriber.
pg_ctl -D /var/lib/postgresql/17/standby stop
pg_createsubscriber \
  -D /var/lib/postgresql/17/standby \
  --publisher-server='host=primary port=5432 dbname=app user=replicator' \
  --subscriber-server='host=standby port=5432 dbname=app user=postgres' \
  --database=app
pg_ctl -D /var/lib/postgresql/17/standby start
```

## 12. pg_dump --filter (v16+)

```bash
# v16+. Allows include/exclude rules from a file.
cat > dump.filter <<EOF
include table public.users
exclude table public.audit_log
EOF

pg_dump --filter=dump.filter -Fc -d app -f app.dump
```

## 13. Non-decimal literals (v16+)

```sql
-- v16+. Hex, octal, binary, with optional underscore separators.
SELECT 0x42F        AS hex,
       0o273        AS oct,
       0b100101     AS bin,
       1_000_000    AS readable_million;
```

## 14. Regex helper functions (v15+)

```sql
-- v15+. Cross-platform regex helpers.
SELECT regexp_count('abc123abc456abc', 'abc'),
       regexp_instr('user@example.com', '@'),
       regexp_like('foo BAR', 'bar', 'i'),
       regexp_substr('order-42-abc', '\d+');
```

## 15. pg_stat_io (v16+)

```sql
-- v16+. Aggregates I/O activity per backend type and object type.
SELECT backend_type, object, context, reads, writes, extends
FROM pg_stat_io
WHERE reads > 0 OR writes > 0
ORDER BY backend_type, object;
```

## 16. pg_maintain role (v17+)

```sql
-- v17+. Allow a non-superuser to run VACUUM/ANALYZE/REINDEX/CLUSTER/REFRESH MV/LOCK TABLE.
GRANT pg_maintain TO maint_user;

-- per-table alternative (v17+) :
GRANT MAINTAIN ON TABLE orders TO maint_user;
```

## 17. Direct TLS handshake (v17+)

```
# v17+ libpq option. Skips the GSSAPI/SSL negotiation round-trip.
psql 'host=db.example.com port=5432 dbname=app user=app sslmode=require sslnegotiation=direct'
```

## Sources

All examples cross-checked against :
- https://www.postgresql.org/docs/release/17.0/
- https://www.postgresql.org/docs/release/16.0/
- https://www.postgresql.org/docs/release/15.0/
- https://www.postgresql.org/docs/17/sql-merge.html
- https://www.postgresql.org/docs/17/functions-json.html
- https://www.postgresql.org/docs/17/sql-copy.html
- https://www.postgresql.org/docs/17/app-pgbasebackup.html
- https://www.postgresql.org/docs/17/app-pgdump.html
- https://www.postgresql.org/docs/17/predefined-roles.html
- https://www.postgresql.org/docs/17/runtime-config-client.html
