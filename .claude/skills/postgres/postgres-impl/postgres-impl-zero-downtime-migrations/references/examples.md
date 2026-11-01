# examples : zero-downtime migrations

Working migration code for postgres-impl-zero-downtime-migrations. Every
snippet is version-annotated. Verified against PostgreSQL 17 documentation;
behaviour is identical on 15, 16, and 17 except where noted.

## 1. Safe migration preamble (v15+)

```sql
-- Run at the start of every migration session.
SET lock_timeout = '3s';        -- fail fast instead of stalling traffic
SET statement_timeout = '60s';  -- bound the operation once it has the lock
```

A migration runner wraps each DDL statement in retry logic : on SQLSTATE
`55P03` (`lock_not_available`) it waits a few seconds and retries, so a
transient long-running query does not abort the deploy.

## 2. Add a column, no rewrite (v15+)

```sql
SET lock_timeout = '3s';

-- Nullable, no default: metadata only.
ALTER TABLE orders ADD COLUMN tracking_code text;

-- Constant / non-volatile default: still metadata only, no rewrite.
ALTER TABLE orders ADD COLUMN status text NOT NULL DEFAULT 'pending';

RESET lock_timeout;
```

For a per-row value such as a creation timestamp, do NOT use
`DEFAULT now()` (volatile, forces a rewrite). Add the column without a
default and backfill it (example 6).

## 3. Build an index concurrently and verify (v15+)

```sql
-- NOT inside BEGIN/COMMIT: CONCURRENTLY cannot run in a transaction block.
CREATE INDEX CONCURRENTLY idx_orders_customer ON orders (customer_id);

-- Confirm the build succeeded.
SELECT indexrelid::regclass AS index_name, indisvalid
FROM pg_index
WHERE indexrelid = 'idx_orders_customer'::regclass;
```

```sql
-- List every INVALID index in the database (failed concurrent builds).
SELECT indexrelid::regclass AS index_name
FROM pg_index
WHERE NOT indisvalid;
```

```sql
-- Recover from a failed build.
DROP INDEX CONCURRENTLY idx_orders_customer;   -- then retry the CREATE
-- or, to rebuild in place (v12+):
REINDEX INDEX CONCURRENTLY idx_orders_customer;
```

## 4. Concurrent index on a partitioned table (v15+)

```sql
-- A concurrent build is not supported on the partitioned table directly.
-- Build each partition's index concurrently:
CREATE INDEX CONCURRENTLY idx_events_2024_ts ON events_2024 (created_at);
CREATE INDEX CONCURRENTLY idx_events_2025_ts ON events_2025 (created_at);

-- Then create the partitioned index non-concurrently. It attaches the
-- matching per-partition indexes instead of rebuilding them, so the
-- write-blocking window stays short.
CREATE INDEX idx_events_ts ON events (created_at);
```

## 5. Add NOT NULL without a blocking scan (v15+)

```sql
-- Step 1: NOT VALID CHECK -- no scan, brief ACCESS EXCLUSIVE for metadata.
ALTER TABLE orders ADD CONSTRAINT orders_status_nn
  CHECK (status IS NOT NULL) NOT VALID;

-- Step 2: VALIDATE -- scans under SHARE UPDATE EXCLUSIVE, writes continue.
ALTER TABLE orders VALIDATE CONSTRAINT orders_status_nn;

-- Step 3: SET NOT NULL -- scan skipped, the valid CHECK already proves it.
ALTER TABLE orders ALTER COLUMN status SET NOT NULL;

-- Step 4 (optional): the CHECK is redundant once NOT NULL is set.
ALTER TABLE orders DROP CONSTRAINT orders_status_nn;
```

If the column still has NULLs, step 2 fails. Backfill the NULLs first
(example 6), then validate.

## 6. Add a foreign key without a blocking scan (v15+)

```sql
-- Step 1: NOT VALID -- enforced for new rows immediately, no scan.
ALTER TABLE orders ADD CONSTRAINT orders_customer_fk
  FOREIGN KEY (customer_id) REFERENCES customers (id) NOT VALID;

-- Step 2: VALIDATE -- SHARE UPDATE EXCLUSIVE on orders,
--                     ROW SHARE on customers.
ALTER TABLE orders VALIDATE CONSTRAINT orders_customer_fk;
```

## 7. Chunked backfill (v15+)

```sql
-- One committed batch, driven by the migration runner in a loop.
-- :lo and :hi advance by the batch size (e.g. 10000) each iteration.
UPDATE orders
SET region = derive_region(country_code)
WHERE id BETWEEN :lo AND :hi
  AND region IS NULL;
```

Server-side loop variant when no external runner is available :

```sql
DO $$
DECLARE
  batch  bigint := 10000;
  rows   bigint;
BEGIN
  LOOP
    UPDATE orders
    SET region = derive_region(country_code)
    WHERE id IN (
      SELECT id FROM orders
      WHERE region IS NULL
      ORDER BY id
      LIMIT batch
    );
    GET DIAGNOSTICS rows = ROW_COUNT;
    EXIT WHEN rows = 0;
    COMMIT;                       -- commit each batch (v11+ allows COMMIT in DO)
  END LOOP;
END $$;
```

Each batch is short, so row locks are released quickly, autovacuum reclaims
the dead tuples between batches, and replication lag stays bounded.

## 8. Expand-contract : change a column type (v15+)

Goal : change `orders.amount` from `numeric` to `bigint` (cents) with no
downtime and no `ALTER COLUMN TYPE` rewrite.

```sql
-- EXPAND: add the new column, nullable, no default -> metadata only.
ALTER TABLE orders ADD COLUMN amount_cents bigint;
```

```sql
-- DUAL-WRITE: keep both columns in sync while the app still writes `amount`.
CREATE FUNCTION sync_amount_cents() RETURNS trigger AS $$
BEGIN
  NEW.amount_cents := round(NEW.amount * 100);
  RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER trg_sync_amount_cents
  BEFORE INSERT OR UPDATE OF amount ON orders
  FOR EACH ROW EXECUTE FUNCTION sync_amount_cents();
```

```sql
-- BACKFILL: chunked UPDATE for rows that predate the trigger (example 7).
UPDATE orders
SET amount_cents = round(amount * 100)
WHERE id BETWEEN :lo AND :hi
  AND amount_cents IS NULL;
```

```sql
-- CONSTRAIN: add NOT NULL via the NOT VALID + VALIDATE pattern (example 5).
-- SWITCH:    deploy application code to read/write amount_cents directly.

-- CONTRACT: remove the old column and the sync trigger.
DROP TRIGGER trg_sync_amount_cents ON orders;
DROP FUNCTION sync_amount_cents();
ALTER TABLE orders DROP COLUMN amount;       -- metadata only
```

Each step is individually safe and reversible. A plain column rename follows
the same expand-contract shape : add the new column, dual-write, backfill,
switch reads, drop the old column. Never rely on a bare `RENAME COLUMN` for
a live system, because the schema rename and the application deploy cannot
happen at the same instant.

## 9. Detach a partition online (v15+)

```sql
-- v14+: reduced lock, two internal transactions, writes to the
-- partitioned table continue. Cannot run inside a transaction block.
ALTER TABLE events DETACH PARTITION events_2023 CONCURRENTLY;
```

## Sources

- https://www.postgresql.org/docs/17/sql-altertable.html
- https://www.postgresql.org/docs/17/sql-createindex.html
- https://www.postgresql.org/docs/17/sql-reindex.html
- https://www.postgresql.org/docs/17/plpgsql-transactions.html
