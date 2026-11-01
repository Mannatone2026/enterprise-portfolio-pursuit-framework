# Reference : Worked Query Reviews + Per-Check Fixes

Complete review examples and before/after fixes. SQL verified against
PostgreSQL 17 docs. Annotation `v15+` means valid across the supported range
15/16/17. Last verified : 2026-05-19.

## 1. Full worked review : an order-history query

Query under review (`reports/order_history.sql:12`) :

```sql
SELECT *
FROM orders o
JOIN order_lines l ON l.order_id = o.id
WHERE lower(o.customer_email) = 'a@b.com'
  AND o.status = 1
ORDER BY o.created_at DESC
LIMIT 20 OFFSET 100000;
```

Review output :

```
SQL Review : reports/order_history.sql:12

[WARNING] SELECT list
  Problem : SELECT * over a JOIN ships every column of both tables and blocks
            index-only scans; it breaks when a column is added to either table.
  Fix     : Enumerate the columns the report actually uses.
  Cite    : postgres-syntax-dml-ddl

[WARNING] WHERE lower(o.customer_email) = 'a@b.com'
  Problem : lower() on the column cannot use a plain B-tree on customer_email.
  Fix     : CREATE INDEX ON orders ((lower(customer_email)));
            (confirm with EXPLAIN (ANALYZE, BUFFERS))
  Cite    : postgres-impl-indexing-strategy

[WARNING] WHERE o.status = 1
  Problem : status is likely a text/enum column; comparing to integer 1 forces
            an implicit cast per row and suppresses any index on status.
  Fix     : Compare to the correct type, e.g. o.status = 'open'.
  Cite    : postgres-impl-indexing-strategy

[WARNING] LIMIT 20 OFFSET 100000
  Problem : OFFSET 100000 reads and discards 100000 rows on every page load.
  Fix     : Keyset pagination on (created_at, id) -- see section 8 below.
  Cite    : postgres-impl-query-performance-toolkit

Verdict : APPROVE WITH WARNINGS   (0 CRITICAL, 4 WARNING, 0 INFO)
```

## 2. C-1 : SELECT * (v15+)

```sql
-- BEFORE : breaks on schema change, over-fetches, no index-only scan
SELECT * FROM customers WHERE id = $1;

-- AFTER : explicit, stable, index-only-scan eligible
SELECT id, name, email FROM customers WHERE id = $1;
```

## 3. C-2 : missing WHERE -- CRITICAL (v15+)

```sql
-- BEFORE : mutates EVERY row
UPDATE invoices SET status = 'paid';

-- AFTER : scoped to the intended rows
UPDATE invoices SET status = 'paid' WHERE id = $1;
```

If a full-table change really is intended, the author must say so explicitly;
the reviewer still reports it as CRITICAL until then.

## 4. C-3 : implicit cast (v15+)

```sql
-- BEFORE : sku is varchar; literal 4071 casts the column per row
SELECT * FROM products WHERE sku = 4071;

-- AFTER : cast the literal, column stays sargable
SELECT id, name FROM products WHERE sku = '4071';
```

## 5. C-4 : JSONB containment without GIN (v15+)

```sql
-- BEFORE : Seq Scan, no index serves @>
SELECT id FROM events WHERE payload @> '{"type":"login"}';

-- FIX : jsonb_path_ops GIN -- smaller and faster for @>-only workloads
CREATE INDEX idx_events_payload ON events USING GIN (payload jsonb_path_ops);

-- If the key-exists operators (?, ?|, ?&) are also needed, use default ops:
CREATE INDEX idx_events_payload ON events USING GIN (payload);
```

## 6. C-5 : function on an indexed column (v15+)

```sql
-- BEFORE : plain index on email does not serve lower(email)
SELECT id FROM users WHERE lower(email) = lower($1);

-- FIX : an expression index matching the predicate (function must be IMMUTABLE)
CREATE INDEX idx_users_email_lower ON users ((lower(email)));
ANALYZE users;   -- expression indexes need statistics
```

## 7. C-6 : leading-wildcard LIKE (v15+)

```sql
-- BEFORE : leading % makes a B-tree useless -> Seq Scan
SELECT id FROM articles WHERE title LIKE '%postgres%';

-- FIX A : trigram GIN index for substring search
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX idx_articles_title_trgm ON articles USING GIN (title gin_trgm_ops);
-- the same LIKE '%postgres%' now uses the trigram index

-- FIX B : full-text search for word search
SELECT id FROM articles
WHERE to_tsvector('english', title) @@ to_tsquery('english', 'postgres');
```

## 8. C-7 + C-8 : N+1 and OFFSET pagination (v15+)

```sql
-- C-7 BEFORE (pseudocode) : one query per order row
-- for order in orders:  SELECT * FROM order_lines WHERE order_id = order.id

-- C-7 AFTER : a single set-based query
SELECT * FROM order_lines WHERE order_id = ANY($1);   -- $1 is an int[]

-- C-8 BEFORE : deep page, OFFSET discards 100k rows
SELECT id, created_at FROM orders ORDER BY created_at DESC LIMIT 20 OFFSET 100000;

-- C-8 AFTER : keyset / seek pagination, carries the last seen key
SELECT id, created_at
FROM orders
WHERE (created_at, id) < ($last_created_at, $last_id)
ORDER BY created_at DESC, id DESC
LIMIT 20;
```

## 9. C-9 : window function vs GROUP BY (v15+)

```sql
-- INFO : window keeps every row; if only the per-customer total is consumed,
-- GROUP BY produces fewer rows and is cheaper.
SELECT customer_id, SUM(amount) OVER (PARTITION BY customer_id) AS total
FROM orders;                         -- one row per order

SELECT customer_id, SUM(amount) AS total
FROM orders GROUP BY customer_id;    -- one row per customer
```

## 10. C-10 : NOT IN with NULLs -- CRITICAL (v15+)

```sql
-- BEFORE : if any orders.customer_id is NULL, this returns ZERO rows, always
SELECT id FROM customers
WHERE id NOT IN (SELECT customer_id FROM orders);

-- AFTER : NOT EXISTS is NULL-safe and plans as an anti-join
SELECT c.id FROM customers c
WHERE NOT EXISTS (SELECT 1 FROM orders o WHERE o.customer_id = c.id);
```

## 11. C-11 : ORDER BY without LIMIT (v15+)

```sql
-- BEFORE : sorts the whole table, spills to disk, consumer reads only the head
SELECT id, created_at FROM events ORDER BY created_at DESC;

-- AFTER : bounded sort -> planner uses top-N heapsort
SELECT id, created_at FROM events ORDER BY created_at DESC LIMIT 50;
```

## 12. C-12 : missing index on a hot column (v15+)

```sql
-- Suspected: orders.customer_id is filtered constantly with no index.
-- Confirm first:
EXPLAIN (ANALYZE, BUFFERS)
SELECT id FROM orders WHERE customer_id = $1;
-- A Seq Scan with a large "Rows Removed by Filter" confirms the miss.

-- FIX :
CREATE INDEX idx_orders_customer_id ON orders (customer_id);
```

## 13. C-13 : fan-out masked by DISTINCT (v15+)

```sql
-- BEFORE : the join multiplies orders by their line count;
-- DISTINCT hides it, and a SUM here would be inflated.
SELECT DISTINCT o.id, o.customer_id
FROM orders o
JOIN order_lines l ON l.order_id = o.id;

-- AFTER : aggregate the child in a subquery, then join 1:1
SELECT o.id, o.customer_id, x.line_count
FROM orders o
JOIN (
  SELECT order_id, count(*) AS line_count
  FROM order_lines GROUP BY order_id
) x ON x.order_id = o.id;
```

## 14. A clean query : verdict APPROVE (v15+)

```sql
SELECT o.id, o.created_at, o.total
FROM orders o
WHERE o.customer_id = $1
  AND o.status = 'open'
ORDER BY o.created_at DESC
LIMIT 20;
```

```
SQL Review : clean example
(no findings)
Verdict : APPROVE   (0 CRITICAL, 0 WARNING, 0 INFO)
```

Explicit columns, sargable typed predicates, bounded `LIMIT`. Assuming an index
on `orders (customer_id, created_at)` exists, this query needs no changes.
