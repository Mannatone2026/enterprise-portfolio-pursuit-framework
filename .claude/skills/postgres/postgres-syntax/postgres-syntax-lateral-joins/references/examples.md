# Examples : LATERAL Joins

Verified, version-annotated examples. All examples target PostgreSQL 15, 16, or 17 unless noted.

## Example 1 : Top-3 Most Recent Posts per User

```sql
-- Schema
CREATE TABLE users (
  id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  email text   NOT NULL UNIQUE
);
CREATE TABLE posts (
  id         bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  user_id    bigint      NOT NULL REFERENCES users(id),
  title      text        NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

-- Critical index for the LATERAL form to be fast :
CREATE INDEX posts_user_created_desc ON posts (user_id, created_at DESC);

-- Top-3 per user, INCLUDING users with zero posts :
SELECT u.id, u.email,
       p.id AS post_id, p.title, p.created_at
FROM users u
LEFT JOIN LATERAL (
  SELECT id, title, created_at
  FROM posts
  WHERE posts.user_id = u.id
  ORDER BY created_at DESC
  LIMIT 3
) AS p ON true
ORDER BY u.id, p.created_at DESC NULLS LAST;
```

Expected plan (verify with `EXPLAIN ANALYZE`) :

```
Nested Loop Left Join
  -> Seq Scan on users u
  -> Limit
       -> Index Scan using posts_user_created_desc on posts
            Index Cond: (user_id = u.id)
```

One inner index-range-scan per outer user, reading at most 3 inner rows per user. Compare to the window-function form which scans every post for every user.

Verified against : postgresql.org/docs/17/queries-table-expressions.html.

## Example 2 : Same Top-N Via Window Function (For Comparison)

```sql
-- Functionally equivalent but slower without the right index :
WITH ranked AS (
  SELECT id, user_id, title, created_at,
         row_number() OVER (PARTITION BY user_id ORDER BY created_at DESC) AS rn
  FROM posts
)
SELECT u.id, u.email, r.id AS post_id, r.title, r.created_at
FROM users u
LEFT JOIN ranked r ON r.user_id = u.id AND r.rn <= 3
ORDER BY u.id, r.created_at DESC NULLS LAST;
```

Expected plan :

```
Hash Right Join
  -> WindowAgg
       -> Sort
            -> Seq Scan on posts   -- reads EVERY post
  -> Seq Scan on users
```

This scans every post in the table, sorts them, computes a window. Inferior to LATERAL+LIMIT for ANY index-friendly partition column.

Verified against : postgresql.org/docs/17/tutorial-window.html, postgresql.org/docs/17/queries-table-expressions.html.

## Example 3 : INNER vs LEFT JOIN LATERAL : the Row-Loss Pitfall

```sql
-- 100 users, 70 of them have no posts.

-- WRONG : INNER form drops users without posts
SELECT u.id, u.email, p.title
FROM users u, LATERAL (
  SELECT title FROM posts
  WHERE posts.user_id = u.id
  ORDER BY created_at DESC LIMIT 1
) AS p;
-- Returns 30 rows. Users without any post are silently absent.

-- RIGHT : LEFT JOIN LATERAL ... ON true preserves outer rows
SELECT u.id, u.email, p.title
FROM users u
LEFT JOIN LATERAL (
  SELECT title FROM posts
  WHERE posts.user_id = u.id
  ORDER BY created_at DESC LIMIT 1
) AS p ON true;
-- Returns 100 rows ; 70 have p.title = NULL.
```

Detection : compare `SELECT count(*) FROM users` to the LATERAL query's row count. If they differ and you expected one-row-per-user, you have the INNER form by mistake.

Verified against : postgresql.org/docs/17/queries-table-expressions.html.

## Example 4 : Expand JSON Array per Row

```sql
CREATE TABLE event (
  id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  payload jsonb  NOT NULL
);
INSERT INTO event (payload) VALUES
  ('{"items":[{"sku":"A","qty":3},{"sku":"B","qty":12},{"sku":"C","qty":7}]}'),
  ('{"items":[]}'),
  ('{}');

-- Inner form : events without items are dropped
SELECT e.id, item->>'sku' AS sku, (item->>'qty')::int AS qty
FROM event e,
     LATERAL jsonb_array_elements(e.payload->'items') AS item;
-- 3 rows (the items in event 1) ; events 2 and 3 are gone.

-- Outer form : every event survives, NULL inner columns when no items
SELECT e.id, item->>'sku' AS sku
FROM event e
LEFT JOIN LATERAL jsonb_array_elements(e.payload->'items') AS item ON true;
-- 5 rows : 3 items for event 1, 1 row for event 2 (NULL sku), 1 row for event 3 (NULL sku).

-- Filtered : only items with qty > 5
SELECT e.id, item->>'sku' AS sku, (item->>'qty')::int AS qty
FROM event e,
     LATERAL jsonb_array_elements(e.payload->'items') AS item
WHERE (item->>'qty')::int > 5;
-- 2 rows : B (qty 12) and C (qty 7) from event 1.

-- v17 equivalent via JSON_TABLE :
SELECT e.id, jt.sku, jt.qty
FROM event e,
  JSON_TABLE(e.payload, '$.items[*]'
    COLUMNS (sku text PATH '$.sku', qty int PATH '$.qty')) AS jt
WHERE jt.qty > 5;
```

Verified against : postgresql.org/docs/17/functions-json.html, postgresql.org/docs/17/queries-table-expressions.html.

## Example 5 : Per-Row generate_series for Time Bucketing

```sql
CREATE TABLE subscription (
  id         bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  user_id    bigint      NOT NULL,
  started_at date        NOT NULL,
  ended_at   date        NOT NULL
);
INSERT INTO subscription (user_id, started_at, ended_at) VALUES
  (1, '2026-01-01', '2026-01-04'),
  (2, '2026-02-01', '2026-02-01'),
  (3, '2026-03-10', '2026-03-09');  -- invalid range (ended before start)

-- One row per active day :
SELECT s.id, s.user_id, d::date AS active_day
FROM subscription s,
     LATERAL generate_series(s.started_at, s.ended_at, '1 day'::interval) AS d
ORDER BY s.id, d;
-- 4 rows for s.id=1 (4 days), 1 row for s.id=2 (1 day), 0 rows for s.id=3.

-- Keep invalid-range subscriptions in the result :
SELECT s.id, s.user_id, d::date AS active_day
FROM subscription s
LEFT JOIN LATERAL generate_series(s.started_at, s.ended_at, '1 day'::interval) AS d ON true
ORDER BY s.id, d NULLS FIRST;
-- 6 rows : 4 + 1 + 1 (the invalid-range row with d = NULL).
```

Verified against : postgresql.org/docs/17/functions-srf.html (generate_series).

## Example 6 : LATERAL With a Set-Returning Custom Function

```sql
-- A function that returns the top-K orders for a customer
CREATE FUNCTION get_recent_orders(p_customer_id bigint, p_limit int)
RETURNS TABLE (id bigint, ordered_at timestamptz, total_amount numeric)
LANGUAGE sql STABLE AS $$
  SELECT id, ordered_at, total_amount
  FROM sales_order
  WHERE customer_id = p_customer_id
  ORDER BY ordered_at DESC
  LIMIT p_limit
$$;

-- Use it per customer (the LATERAL keyword is OPTIONAL for function calls,
-- but explicit is clearer)
SELECT c.id, c.email, r.id AS order_id, r.ordered_at, r.total_amount
FROM customer c,
     LATERAL get_recent_orders(c.id, 3) AS r;

-- Or LEFT-outer to keep customers without recent orders
SELECT c.id, c.email, r.id AS order_id
FROM customer c
LEFT JOIN LATERAL get_recent_orders(c.id, 3) AS r ON true;
```

Verified against : postgresql.org/docs/17/xfunc-sql.html (set-returning SQL functions), postgresql.org/docs/17/queries-table-expressions.html.

## Example 7 : LATERAL Chain (Two LATERALs Referencing Each Other)

```sql
-- Canonical docs example : polygons whose vertices come close to each other
CREATE TABLE polygons (id int PRIMARY KEY, poly polygon);

SELECT p1.id, p2.id, v1, v2
FROM polygons p1, polygons p2,
     LATERAL vertices(p1.poly) v1,
     LATERAL vertices(p2.poly) v2
WHERE (v1 <-> v2) < 10 AND p1.id != p2.id;
-- Cross product of polygon pairs, then vertices per polygon, filtered by distance.
-- vertices(p1.poly) and vertices(p2.poly) each evaluate per outer row pair.
```

Verified against : postgresql.org/docs/17/queries-table-expressions.html (LATERAL example).

## Example 8 : LATERAL With Aggregation : Multi-Column Replacement of Correlated Subqueries

```sql
-- Two correlated subqueries (two passes over sales_order per customer) :
SELECT c.id, c.email,
       (SELECT count(*)        FROM sales_order o WHERE o.customer_id = c.id) AS order_count,
       (SELECT sum(total_amount) FROM sales_order o WHERE o.customer_id = c.id) AS order_sum
FROM customer c;

-- One LATERAL pass per customer (planner can sometimes prove equivalence anyway,
-- but the LATERAL form is explicit about the single-pass intent) :
SELECT c.id, c.email, agg.order_count, agg.order_sum
FROM customer c,
     LATERAL (
       SELECT count(*) AS order_count, sum(total_amount) AS order_sum
       FROM sales_order
       WHERE customer_id = c.id
     ) AS agg;

-- LEFT-outer keeps customers with no orders (count = 0, sum = NULL) :
SELECT c.id, c.email, coalesce(agg.order_count, 0) AS order_count, agg.order_sum
FROM customer c
LEFT JOIN LATERAL (
  SELECT count(*) AS order_count, sum(total_amount) AS order_sum
  FROM sales_order
  WHERE customer_id = c.id
) AS agg ON true;
```

NOTE : aggregate over an empty result set returns one row (count = 0, sum = NULL). Wrapping in LEFT JOIN LATERAL is unnecessary for `count(*)` because the inner subquery always returns exactly one row. It IS necessary when the inner subquery has its own LIMIT that can yield zero rows.

Verified against : postgresql.org/docs/17/functions-aggregate.html, postgresql.org/docs/17/queries-table-expressions.html.

## Example 9 : LATERAL With Memoize (v14+)

```sql
-- Memoize caches the inner Nested-Loop result per outer key.
-- Useful when outer rows repeat the same join key.
EXPLAIN (ANALYZE, BUFFERS)
SELECT e.event_type, c.country
FROM events e,
     LATERAL (
       SELECT country FROM countries c WHERE c.code = e.country_code
     ) c;
-- Expected (v14+) :
-- Nested Loop
--   -> Seq Scan on events
--   -> Memoize
--        Cache Key: e.country_code
--        -> Index Scan using countries_pkey on countries
--             Index Cond: code = e.country_code
```

`Memoize` is automatic when the planner expects high repetition. Disable with `SET enable_memoize = off` for benchmarking.

Verified against : postgresql.org/docs/17/runtime-config-query.html (`enable_memoize`), postgresql.org/docs/release/14.0/.
