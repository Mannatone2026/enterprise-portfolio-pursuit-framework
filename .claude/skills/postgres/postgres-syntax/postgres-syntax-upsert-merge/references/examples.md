# UPSERT and MERGE Examples Reference

Ten complete, runnable patterns for `INSERT ... ON CONFLICT` and `MERGE` on PostgreSQL 15, 16, and 17. Every example verified against `postgresql.org/docs/17/sql-insert.html` and `postgresql.org/docs/17/sql-merge.html` on 2026-05-19.

## Example 1 : Idempotent Insert (DO NOTHING) :

Skip duplicate rows without error, return the actually-inserted rows :

```sql
CREATE TABLE users (
  email      text PRIMARY KEY,
  name       text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO users (email, name) VALUES
  ('alice@ex.com', 'Alice'),
  ('bob@ex.com',   'Bob'),
  ('alice@ex.com', 'Alice (dup)')
ON CONFLICT (email) DO NOTHING
RETURNING email, name;
-- Returns 2 rows (Alice, Bob). The duplicate is silently skipped.
```

## Example 2 : Upsert With Last-Write-Wins :

Overwrite all columns from the new row when a conflict occurs :

```sql
INSERT INTO users (email, name) VALUES ('alice@ex.com', 'Alice 2')
ON CONFLICT (email) DO UPDATE
SET name       = EXCLUDED.name,
    updated_at = now();
```

`EXCLUDED.name` is the proposed `'Alice 2'`. The existing row is read via `users.name` (or alias).

## Example 3 : Counter Increment (Aggregate Upsert) :

Insert with count=1; if it already exists, add to the existing count :

```sql
CREATE TABLE page_views (
  page_id  bigint PRIMARY KEY,
  hits     bigint NOT NULL,
  last_hit timestamptz NOT NULL
);

INSERT INTO page_views (page_id, hits, last_hit) VALUES (42, 1, now())
ON CONFLICT (page_id) DO UPDATE
SET hits     = page_views.hits + EXCLUDED.hits,
    last_hit = GREATEST(page_views.last_hit, EXCLUDED.last_hit);
```

Both `page_views.hits` (existing) and `EXCLUDED.hits` (proposed) are accessible.

## Example 4 : Partial-Unique-Index UPSERT :

Conditional uniqueness via partial unique index :

```sql
CREATE UNIQUE INDEX users_active_email_uq ON users (email) WHERE is_active;

INSERT INTO users (email, name, is_active) VALUES ('alice@ex.com', 'Alice', true)
ON CONFLICT (email) WHERE is_active DO UPDATE SET name = EXCLUDED.name;
```

The `WHERE is_active` after the conflict target MUST textually match the index's WHERE clause.

## Example 5 : Conditional UPDATE (WHERE In Action) :

Update only if the proposed value is newer :

```sql
INSERT INTO inventory (sku, qty, updated_at) VALUES ('SKU-1', 100, '2026-05-19 12:00')
ON CONFLICT (sku) DO UPDATE
SET qty        = EXCLUDED.qty,
    updated_at = EXCLUDED.updated_at
WHERE inventory.updated_at < EXCLUDED.updated_at;
-- If the existing row is newer, the WHERE evaluates FALSE and nothing is updated.
```

The WHERE in the DO UPDATE acts as a row-level guard. Skipped updates do NOT appear in RETURNING.

## Example 6 : Distinguish INSERT From UPDATE In RETURNING :

`xmax = 0` is true only for freshly inserted rows :

```sql
INSERT INTO users (email, name) VALUES ('alice@ex.com', 'Alice')
ON CONFLICT (email) DO UPDATE SET name = EXCLUDED.name
RETURNING email, (xmax = 0) AS was_inserted;
-- was_inserted = TRUE  → row was newly inserted
-- was_inserted = FALSE → row already existed and was updated
```

The `xmax` system column is the transaction ID that updated the row; for a fresh insert it is 0.

## Example 7 : Simple MERGE (v15+) :

Two-action sync from a staging table :

```sql
CREATE TABLE customers   (id bigint PRIMARY KEY, name text, email text);
CREATE TABLE customer_in (id bigint, name text, email text);

MERGE INTO customers t USING customer_in s ON t.id = s.id
WHEN MATCHED THEN
  UPDATE SET name = s.name, email = s.email
WHEN NOT MATCHED THEN
  INSERT (id, name, email) VALUES (s.id, s.name, s.email);
```

## Example 8 : Full-Sync MERGE (v17+) :

Apply a complete snapshot : INSERT new, UPDATE changed, DELETE missing :

```sql
MERGE INTO customers t USING customer_in s ON t.id = s.id
WHEN MATCHED AND (t.name, t.email) IS DISTINCT FROM (s.name, s.email) THEN
  UPDATE SET name = s.name, email = s.email
WHEN MATCHED THEN
  DO NOTHING                                                 -- unchanged row, skip
WHEN NOT MATCHED THEN
  INSERT (id, name, email) VALUES (s.id, s.name, s.email)
WHEN NOT MATCHED BY SOURCE THEN                              -- v17+
  DELETE;
```

`IS DISTINCT FROM` is NULL-safe (treats NULL as a value). The `DO NOTHING` branch prevents pointless row rewrites that would still bump `xmax`.

## Example 9 : MERGE With RETURNING merge_action() (v17+) :

Emit an audit log of what changed :

```sql
MERGE INTO products t USING product_in s ON t.sku = s.sku
WHEN MATCHED THEN UPDATE SET price = s.price
WHEN NOT MATCHED THEN INSERT (sku, price) VALUES (s.sku, s.price)
RETURNING merge_action() AS action, t.sku, t.price;
-- action | sku    | price
-- INSERT | SKU-A  | 9.99
-- UPDATE | SKU-B  | 14.50
```

Wrap the MERGE in a CTE to push the audit rows downstream :

```sql
WITH changes AS (
  MERGE INTO products t USING product_in s ON t.sku = s.sku
  WHEN MATCHED THEN UPDATE SET price = s.price
  WHEN NOT MATCHED THEN INSERT (sku, price) VALUES (s.sku, s.price)
  RETURNING merge_action() AS action, t.sku, t.price
)
INSERT INTO product_audit (action, sku, price, at) SELECT action, sku, price, now() FROM changes;
```

## Example 10 : Deduplicating MERGE Source :

Prevent `ERROR 21000` (cardinality violation) by aggregating the source first :

```sql
-- BAD : source has duplicates per key
MERGE INTO users t USING raw_events s ON t.id = s.user_id
WHEN MATCHED THEN UPDATE SET last_seen = s.ts;
-- ERROR: MERGE command cannot affect row a second time

-- GOOD : DISTINCT ON keeps one row per user, the latest
MERGE INTO users t USING (
  SELECT DISTINCT ON (user_id) user_id, ts
  FROM raw_events
  ORDER BY user_id, ts DESC
) s ON t.id = s.user_id
WHEN MATCHED THEN UPDATE SET last_seen = s.ts;

-- ALTERNATIVE : GROUP BY when reduction is well-defined
MERGE INTO users t USING (
  SELECT user_id, MAX(ts) AS ts FROM raw_events GROUP BY user_id
) s ON t.id = s.user_id
WHEN MATCHED THEN UPDATE SET last_seen = s.ts;
```

Either pattern (DISTINCT ON or GROUP BY) guarantees at most one source row per target. Pick GROUP BY when the reduction is associative (MAX, SUM, MIN); pick DISTINCT ON when you need a full row's values from a specific source-row.
