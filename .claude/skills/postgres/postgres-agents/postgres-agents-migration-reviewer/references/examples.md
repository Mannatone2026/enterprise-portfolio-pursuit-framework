# postgres-agents-migration-reviewer : examples

Complete worked reviews and unsafe-to-safe rewrites per check. Verified against
PostgreSQL 17 (sql-altertable.html, sql-createindex.html). Behavior is the same
on v15, v16, v17 unless a version note says otherwise.

## Worked review 1 : an unsafe migration

Migration under review :

```sql
BEGIN;
CREATE INDEX idx_orders_customer ON orders (customer_id);
ALTER TABLE orders ADD COLUMN status text NOT NULL;
COMMIT;
```

Review output :

```
[CRITICAL] M-1 : statement 2, CREATE INDEX idx_orders_customer
  Problem : plain CREATE INDEX locks orders against INSERT/UPDATE/DELETE for
            the whole build. Every write to orders blocks until it finishes.
  Fix     : CREATE INDEX CONCURRENTLY idx_orders_customer ON orders (customer_id);
            run it outside any transaction block.
  Cite    : postgres-impl-zero-downtime-migrations

[CRITICAL] M-11 : statement 2 inside BEGIN/COMMIT
  Problem : once rewritten to CONCURRENTLY, the index build cannot run inside
            this transaction block; the migration will error out.
  Fix     : move the CONCURRENTLY build out of the transaction; split the
            migration so the index is its own non-transactional step.
  Cite    : postgres-impl-zero-downtime-migrations

[CRITICAL] M-4 : statement 3, ADD COLUMN status text NOT NULL
  Problem : orders is populated; every existing row violates NOT NULL with no
            DEFAULT, so the statement fails.
  Fix     : ADD COLUMN status text;  -- nullable
            backfill status in chunks;
            then add NOT NULL via the NOT VALID CHECK path (M-5).
  Cite    : postgres-impl-zero-downtime-migrations

[WARNING] M-8 : whole migration
  Problem : no lock_timeout is set; if the ALTER cannot get its ACCESS
            EXCLUSIVE lock it waits forever and queues all traffic behind it.
  Fix     : SET lock_timeout = '5s'; at the top of each locking step.
  Cite    : postgres-errors-statement-timeouts

Verdict : UNSAFE   (3 CRITICAL, 1 WARNING, 0 INFO)
```

## Worked review 2 : a safe migration

Migration under review :

```sql
SET lock_timeout = '5s';
ALTER TABLE invoices ADD COLUMN archived boolean;
ALTER TABLE invoices ALTER COLUMN archived SET DEFAULT false;
```

Review output :

```
Verdict : SAFE   (0 CRITICAL, 0 WARNING, 0 INFO)
```

Notes recorded by the reviewer : `ADD COLUMN` with no default is metadata only.
`SET DEFAULT` afterwards does not touch existing rows. `lock_timeout` guards the
brief `ACCESS EXCLUSIVE` lock. Nothing fires.

## Unsafe-to-safe rewrites per check :

### M-1 / M-11 : CREATE INDEX

```sql
-- UNSAFE: blocks writes; also illegal inside a transaction once made concurrent
CREATE INDEX idx ON events (user_id);

-- SAFE (v15+): own statement, no transaction wrapper
CREATE INDEX CONCURRENTLY idx ON events (user_id);
-- if it fails it leaves an INVALID index:
--   DROP INDEX CONCURRENTLY idx;  then retry
```

### M-2 : ALTER COLUMN TYPE

```sql
-- UNSAFE: full table + index rewrite under ACCESS EXCLUSIVE
ALTER TABLE payments ALTER COLUMN amount TYPE numeric(14,2);

-- SAFE: expand-contract
ALTER TABLE payments ADD COLUMN amount_v2 numeric(14,2);          -- expand
-- backfill amount_v2 in chunks (see M-9), keep it in sync with a trigger
ALTER TABLE payments RENAME COLUMN amount   TO amount_old;        -- swap
ALTER TABLE payments RENAME COLUMN amount_v2 TO amount;
ALTER TABLE payments DROP COLUMN amount_old;                      -- contract
```

### M-3 : ADD COLUMN with a volatile DEFAULT

```sql
-- UNSAFE: volatile default rewrites the whole table
ALTER TABLE sessions ADD COLUMN created_at timestamptz DEFAULT now();

-- SAFE: nullable add, chunked backfill, then set the default
ALTER TABLE sessions ADD COLUMN created_at timestamptz;
-- backfill created_at in chunks
ALTER TABLE sessions ALTER COLUMN created_at SET DEFAULT now();
```

### M-5 : SET NOT NULL

```sql
-- UNSAFE: scans the whole table under ACCESS EXCLUSIVE
ALTER TABLE users ALTER COLUMN email SET NOT NULL;

-- SAFE: prove no NULL with a validated CHECK first, then SET NOT NULL skips its scan
ALTER TABLE users ADD CONSTRAINT users_email_nn CHECK (email IS NOT NULL) NOT VALID;
ALTER TABLE users VALIDATE CONSTRAINT users_email_nn;   -- SHARE UPDATE EXCLUSIVE
ALTER TABLE users ALTER COLUMN email SET NOT NULL;      -- scan skipped
```

### M-6 : ADD FOREIGN KEY

```sql
-- UNSAFE: validates every existing row while holding a write-blocking lock
ALTER TABLE orders ADD CONSTRAINT orders_customer_fk
  FOREIGN KEY (customer_id) REFERENCES customers (id);

-- SAFE: two-step NOT VALID then VALIDATE
ALTER TABLE orders ADD CONSTRAINT orders_customer_fk
  FOREIGN KEY (customer_id) REFERENCES customers (id) NOT VALID;  -- fast, metadata
ALTER TABLE orders VALIDATE CONSTRAINT orders_customer_fk;        -- no write block
```

### M-9 : chunked backfill

```sql
-- UNSAFE: one statement locks every row and bloats the table
UPDATE events SET processed = true WHERE processed IS NULL;

-- SAFE: chunked, each batch its own transaction
-- repeat until zero rows updated:
UPDATE events SET processed = true
WHERE id IN (
  SELECT id FROM events WHERE processed IS NULL ORDER BY id LIMIT 10000
);
```

### M-7 : DROP / RENAME (expand-contract ordering)

```sql
-- UNSAFE: old app code references old_name and breaks at COMMIT
ALTER TABLE clients RENAME COLUMN fullname TO full_name;

-- SAFE: sequence schema and app deploys
-- 1. expand:  ADD COLUMN full_name; backfill; sync with a trigger
-- 2. deploy:  ship app code that writes/reads full_name
-- 3. contract: once no code uses fullname, DROP COLUMN fullname
```

### M-14 : index on a partitioned table

```sql
-- UNSAFE: cascades a non-concurrent build to every partition, locking them all
CREATE INDEX idx ON measurements (taken_at);

-- SAFE (v15+): build per partition concurrently, then attach
CREATE INDEX CONCURRENTLY idx_2024 ON measurements_2024 (taken_at);
CREATE INDEX CONCURRENTLY idx_2025 ON measurements_2025 (taken_at);
CREATE INDEX idx ON ONLY measurements (taken_at);          -- parent, no data
ALTER INDEX idx ATTACH PARTITION idx_2024;
ALTER INDEX idx ATTACH PARTITION idx_2025;
```

## Verdict examples :

```
Verdict : SAFE            (0 CRITICAL, 0 WARNING, 0 INFO)
Verdict : NEEDS-CHANGES   (0 CRITICAL, 2 WARNING, 1 INFO)
Verdict : UNSAFE          (1 CRITICAL, 0 WARNING, 0 INFO)
```

A single CRITICAL forces UNSAFE even when everything else is clean. WARNING
findings with no CRITICAL produce NEEDS-CHANGES. INFO never changes the verdict.
