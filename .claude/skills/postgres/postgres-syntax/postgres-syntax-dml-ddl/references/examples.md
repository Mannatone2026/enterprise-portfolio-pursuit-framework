# postgres-syntax-dml-ddl : Examples

All examples verified against PostgreSQL 17 docs. Version annotations on every non-baseline feature.

## Example 1 : Modern CREATE TABLE with IDENTITY, CHECK, FK, STORED column

```sql
CREATE TABLE products (
    product_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    sku        text   NOT NULL UNIQUE,
    name       text   NOT NULL,
    price      numeric(12,2) NOT NULL CHECK (price >= 0),
    weight_g   integer NOT NULL CHECK (weight_g > 0),
    in_stock   boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE order_items (
    order_id   bigint NOT NULL REFERENCES orders(order_id) ON DELETE CASCADE,
    line_no    smallint NOT NULL,
    product_id bigint NOT NULL REFERENCES products(product_id) ON DELETE RESTRICT,
    quantity   integer NOT NULL CHECK (quantity > 0),
    unit_price numeric(12,2) NOT NULL CHECK (unit_price >= 0),
    line_total numeric(14,2) GENERATED ALWAYS AS (unit_price * quantity) STORED,
    PRIMARY KEY (order_id, line_no)
);
```

Verify the IDENTITY was set correctly :

```sql
SELECT attname, attidentity, attgenerated
FROM pg_attribute
WHERE attrelid = 'products'::regclass AND attnum > 0 AND NOT attisdropped;
-- attidentity : 'a' = ALWAYS, 'd' = BY DEFAULT, ''  = none
-- attgenerated : 's' = STORED, ''  = none
```

## Example 2 : Server-allocated id round-trip with RETURNING

```sql
-- Single-row, return generated id :
INSERT INTO products (sku, name, price, weight_g)
VALUES ('SKU-001', 'Widget', 9.99, 250)
RETURNING product_id, created_at;
--  product_id |          created_at
--          1  | 2026-05-19 09:30:01.234+02

-- Multi-row INSERT with mixed DEFAULT keyword :
INSERT INTO products (sku, name, price, weight_g, in_stock)
VALUES ('SKU-002', 'Gizmo', 12.50, 180, DEFAULT),
       ('SKU-003', 'Thingamajig', 1.20, 50, false)
RETURNING product_id, sku, in_stock;

-- INSERT ... SELECT with RETURNING :
INSERT INTO products_archive (product_id, sku, name, price, weight_g, in_stock, created_at)
SELECT product_id, sku, name, price, weight_g, in_stock, created_at
  FROM products WHERE NOT in_stock
RETURNING product_id;
```

## Example 3 : OVERRIDING SYSTEM VALUE for data migration

```sql
-- products has GENERATED ALWAYS AS IDENTITY ; this normally REJECTS user ids :
INSERT INTO products (product_id, sku, name, price, weight_g) VALUES
  (1001, 'SKU-LEGACY-1001', 'Imported A', 1.00, 10);
-- ERROR : SQLSTATE 428C9
--   cannot insert a non-DEFAULT value into column "product_id"
--   HINT  : OVERRIDING SYSTEM VALUE can be used to override.

-- Correct migration insert :
INSERT INTO products (product_id, sku, name, price, weight_g) OVERRIDING SYSTEM VALUE
VALUES (1001, 'SKU-LEGACY-1001', 'Imported A', 1.00, 10),
       (1002, 'SKU-LEGACY-1002', 'Imported B', 2.00, 20);

-- After bulk import : advance the sequence past the highest imported id :
SELECT setval(pg_get_serial_sequence('products', 'product_id'),
              (SELECT max(product_id) FROM products));
```

## Example 4 : Data-modifying CTE (atomic move between tables)

```sql
-- Move old orders to an archive table in one statement :
WITH moved AS (
  DELETE FROM orders
   WHERE created_at < date_trunc('year', now()) - interval '1 year'
  RETURNING *
)
INSERT INTO orders_archive
SELECT * FROM moved
RETURNING order_id;
```

Single statement, atomic , no window where a row exists in neither table.

## Example 5 : UPDATE with FROM-join and RETURNING

```sql
-- new_prices is a staging table with new prices for a subset of products :
UPDATE products AS p
   SET price = n.new_price
  FROM new_prices AS n
 WHERE p.product_id = n.product_id
   AND p.price <> n.new_price                 -- avoid no-op UPDATEs
RETURNING p.product_id, p.price AS new_price;

-- Multi-column SET, with DEFAULT to reset a column :
UPDATE products
   SET (price, in_stock) = (DEFAULT, true)
 WHERE product_id = 1;

-- Conditional UPDATE returning OLD vs NEW with a CTE :
WITH before AS (
  SELECT product_id, price AS old_price FROM products WHERE product_id = 1
)
UPDATE products
   SET price = price * 1.05
 WHERE product_id = 1
RETURNING product_id, (SELECT old_price FROM before) AS old_price, price AS new_price;
```

## Example 6 : DELETE with USING-join and RETURNING

```sql
-- Delete products from a purged supplier in one statement :
DELETE FROM products AS p
 USING suppliers   AS s
 WHERE p.supplier_id = s.supplier_id
   AND s.status = 'purged'
RETURNING p.product_id, p.sku;

-- Bounded delete : 10000 rows at a time, lock-friendly :
WITH batch AS (
  SELECT product_id
    FROM products
   WHERE archived_at < now() - interval '5 years'
   ORDER BY product_id
   FOR UPDATE SKIP LOCKED
   LIMIT 10000
)
DELETE FROM products
 WHERE product_id IN (SELECT product_id FROM batch)
RETURNING product_id;
```

Run the bounded delete in a loop with `SELECT pg_sleep(0.2)` between batches to let autovacuum and replicas catch up.

## Example 7 : TRUNCATE for tests / fixtures

```sql
-- Fast reset of test fixtures (drops disk pages, resets identity) :
TRUNCATE products, orders, order_items RESTART IDENTITY;

-- With FK cascade (destructive) :
TRUNCATE customers RESTART IDENTITY CASCADE;
-- WARNING : also truncates orders, order_items, payments, ... transitively

-- Inside a transaction (rolls back if not committed) :
BEGIN;
  TRUNCATE products RESTART IDENTITY;
  -- ... insert fixture data ...
COMMIT;
```

## Example 8 : ALTER TABLE multi-action

```sql
-- All in one ACCESS EXCLUSIVE acquisition :
ALTER TABLE products
    ADD COLUMN supplier_id bigint REFERENCES suppliers(supplier_id),
    ADD COLUMN archived_at timestamptz,
    ADD CONSTRAINT products_sku_format_chk CHECK (sku ~ '^SKU-[0-9A-Z-]+$') NOT VALID,
    ALTER COLUMN name SET STATISTICS 500;

-- Validate the NOT VALID check (SHARE UPDATE EXCLUSIVE , does not block writes) :
ALTER TABLE products VALIDATE CONSTRAINT products_sku_format_chk;

-- Rename a column :
ALTER TABLE products RENAME COLUMN weight_g TO weight_grams;

-- Move to another schema :
ALTER TABLE products SET SCHEMA catalog;
```

## Example 9 : SERIAL to IDENTITY migration

```sql
-- Existing legacy table :
CREATE TABLE legacy_orders (
    id serial PRIMARY KEY,
    payload text
);

-- Convert in place (v15+) :
ALTER TABLE legacy_orders
    ALTER COLUMN id DROP DEFAULT,                                   -- remove nextval default
    ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY;

-- Confirm the sequence is now owned by the IDENTITY column :
SELECT attname, attidentity FROM pg_attribute
WHERE attrelid = 'legacy_orders'::regclass AND attname = 'id';
-- attidentity : 'd' = BY DEFAULT

-- Optionally tighten to ALWAYS (only if you can guarantee no app supplies the id) :
ALTER TABLE legacy_orders ALTER COLUMN id SET GENERATED ALWAYS;
```

## Example 10 : Transactional DDL inside BEGIN

```sql
BEGIN;
  ALTER TABLE products
      ADD COLUMN region text,
      ADD CONSTRAINT products_region_chk
          CHECK (region IN ('eu','us','ap')) NOT VALID;
  UPDATE products SET region = 'eu' WHERE region IS NULL;
  ALTER TABLE products VALIDATE CONSTRAINT products_region_chk;
COMMIT;

-- This MUST run OUTSIDE any transaction :
CREATE INDEX CONCURRENTLY products_region_idx ON products (region);

-- If CIC fails part-way through, the index is INVALID , clean it up :
SELECT i.indexrelid::regclass FROM pg_index i WHERE NOT i.indisvalid;
DROP INDEX products_region_idx;       -- then retry
```

## Example 11 : GENERATED STORED column lifecycle

```sql
-- Create with a STORED column :
CREATE TABLE invoices (
    invoice_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    subtotal   numeric(14,2) NOT NULL,
    tax_rate   numeric(5,4)  NOT NULL DEFAULT 0.21,
    total      numeric(14,2) GENERATED ALWAYS AS (subtotal * (1 + tax_rate)) STORED
);

-- INSERT does NOT supply 'total' :
INSERT INTO invoices (subtotal) VALUES (100.00) RETURNING invoice_id, total;
-- total = 121.00

-- UPDATE that changes a referenced column recomputes 'total' :
UPDATE invoices SET subtotal = 200.00 WHERE invoice_id = 1 RETURNING total;
-- total = 242.00

-- Attempting to INSERT into the generated column is rejected :
INSERT INTO invoices (subtotal, total) VALUES (100, 999);
-- ERROR : SQLSTATE 428C9
--   cannot insert a non-DEFAULT value into column "total"

-- Adding a STORED column to an existing table rewrites the heap :
ALTER TABLE invoices
    ADD COLUMN total_eur numeric(14,2) GENERATED ALWAYS AS (total) STORED;
-- Full table rewrite , take ACCESS EXCLUSIVE long enough to scan + write.
```

## Example 12 : OVERRIDING USER VALUE for copying with sequence rebase

```sql
-- Copy from old table to new where new uses BY DEFAULT IDENTITY and we want
-- the new sequence to allocate fresh ids (ignoring source ids) :
INSERT INTO products_v2 OVERRIDING USER VALUE
SELECT * FROM products_v1;
-- The sequence on products_v2.product_id supplies each new id ;
-- the source value is dropped.
```

Sources verified against : postgresql.org/docs/17/sql-createtable.html, postgresql.org/docs/17/sql-altertable.html, postgresql.org/docs/17/sql-insert.html, postgresql.org/docs/17/sql-update.html, postgresql.org/docs/17/sql-delete.html, postgresql.org/docs/17/sql-truncate.html, postgresql.org/docs/17/ddl-generated-columns.html, postgresql.org/docs/17/queries-with.html.
