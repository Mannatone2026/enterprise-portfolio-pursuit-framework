---
name: postgres-syntax-lateral-joins
description: >
  Use when computing per-row dynamic subqueries, top-N-per-group results, or expanding JSON arrays per row in a JOIN clause.
  Prevents losing outer rows when LATERAL subquery returns empty (use LEFT JOIN LATERAL ON true), and reaching for LATERAL when a simple JOIN suffices (less optimizer-friendly).
  Covers LATERAL semantics, INNER vs LEFT JOIN LATERAL, correlated subquery equivalence, top-N-per-group pattern, LATERAL with set-returning functions, LATERAL vs scalar subquery decision tree.
  Keywords: LATERAL, CROSS JOIN LATERAL, LEFT JOIN LATERAL, top N per group, per row subquery, correlated subquery, generate_series LATERAL, unnest in JOIN, missing rows when joining, why do I lose rows in lateral, jsonb_array_elements per row, polygons vertices LATERAL, lateral subquery empty
license: MIT
compatibility: "Designed for Claude Code. Requires PostgreSQL 15, 16, or 17."
metadata:
  author: OpenAEC-Foundation
  version: "1.0"
---

# postgres-syntax-lateral-joins

## Quick Reference :

A `LATERAL` subquery in the `FROM` clause is allowed to reference columns from preceding `FROM` items. Without `LATERAL`, each `FROM` subquery is evaluated once, independent of every other item. With `LATERAL`, the subquery is re-evaluated FOR EACH ROW of the preceding items, with their column values bound. The result rows are then joined with the source rows in the standard JOIN-tree fashion. The keyword is REQUIRED for `(SELECT ...)` subqueries that cross-reference earlier items ; the keyword is OPTIONAL for `function_call(args)` set-returning functions because function arguments may always reference earlier FROM items.

The two dominant join shapes are `FROM outer, LATERAL (...) sub` (implicit CROSS JOIN ; rows where the subquery returns zero rows are DROPPED) and `FROM outer LEFT JOIN LATERAL (...) sub ON true` (LEFT outer ; rows where the subquery returns zero rows are KEPT with NULLs in the sub-columns). Picking the wrong one is the single most common bug : "my query suddenly lost half the rows" is almost always a missing LEFT JOIN LATERAL ON true. LATERAL is mandatory for top-N-per-group (ORDER BY + LIMIT k inside the subquery, correlated to the outer row), for expanding JSON arrays per row (`jsonb_array_elements`), and for per-row computed joins where the inner table's filter depends on the outer row's columns.

## When To Use This Skill :

ALWAYS use this skill when :
- Writing top-N-per-group queries (3 latest posts per user, 5 highest-priced items per category)
- Calling a set-returning function (`unnest`, `generate_series`, `jsonb_array_elements`) with arguments from a preceding FROM item
- Expanding a JSON array column to rows alongside the outer row's other columns
- Joining to a subquery whose predicate depends on the outer row
- Deciding between a scalar subquery, a regular JOIN, and a LATERAL JOIN

NEVER use this skill for :
- Plain equi-joins between two tables (use a normal `JOIN ... ON`)
- Aggregation across groups (use `GROUP BY` ; LATERAL is per-row)
- JSON_TABLE projection (v17+) : see `postgres-syntax-jsonb`
- Window-function top-N (use `row_number() OVER (PARTITION BY ...)`) : see `postgres-syntax-window-functions`

## Decision Trees :

### Scalar subquery, JOIN, or LATERAL? :

```
Subquery returns at most ONE row AND ONE column?
├── Yes : scalar subquery in SELECT list.
│         SELECT a.*, (SELECT count(*) FROM b WHERE b.a_id = a.id) AS cnt FROM a;
│         (cheaper than LATERAL ; planner can pull up to a join when beneficial)
└── No : subquery returns multiple rows OR multiple columns?
    │
    Subquery's WHERE clause references outer row columns?
    ├── No (independent inner result set, simple equi-join) :
    │       regular JOIN with ON.
    │       SELECT ... FROM a JOIN b ON b.a_id = a.id;
    └── Yes (subquery filter/ORDER/LIMIT depends on the outer row) :
        LATERAL.
        ├── Inner row could be empty AND outer row must survive :
        │       LEFT JOIN LATERAL (...) ON true
        └── Drop outer rows when inner is empty :
                FROM a, LATERAL (...) sub   OR   CROSS JOIN LATERAL
```

### Pick the LATERAL join flavor :

```
What should happen when LATERAL subquery returns zero rows?
├── Drop the outer row : `FROM outer, LATERAL (...) sub`
│                      ≡ `FROM outer CROSS JOIN LATERAL (...) sub`
│                      ≡ `FROM outer INNER JOIN LATERAL (...) sub ON true`
└── Keep the outer row, fill sub-columns with NULL :
        `FROM outer LEFT JOIN LATERAL (...) sub ON true`
        (almost always the right choice for "find rows that have no children")
```

### Is the LATERAL keyword required? :

```
FROM item is a subquery `(SELECT ...)` that cross-references prior items?
├── Yes : LATERAL keyword IS REQUIRED. Without it, parser raises 42P01.
└── No : the FROM item is a function call `f(args)`?
    ├── Yes : LATERAL keyword is OPTIONAL (function arguments always see prior items).
    │         Style guidance : write LATERAL anyway when the cross-reference is
    │         load-bearing ; it makes intent explicit.
    └── No : LATERAL keyword is meaningless / not applicable.
```

## Patterns :

### Pattern 1 : Top-N-per-group via `LATERAL ... LIMIT N`

ALWAYS use a LATERAL subquery for top-N-per-group when N is small (≤ a few hundred per group).
NEVER use a window function (`row_number() OVER (PARTITION BY ...) WHERE rn <= N`) when the inner table has a usable B-tree index on `(parent_id, order_col)` : LATERAL+LIMIT is dramatically faster.

```sql
-- 3 most recent posts per user.
-- Index that makes this fast :
-- CREATE INDEX posts_user_created ON posts (user_id, created_at DESC);

SELECT u.id, u.email, p.id AS post_id, p.title, p.created_at
FROM users u
LEFT JOIN LATERAL (
  SELECT id, title, created_at
  FROM posts
  WHERE posts.user_id = u.id
  ORDER BY created_at DESC
  LIMIT 3
) AS p ON true;

-- LEFT JOIN LATERAL ... ON true keeps users who have no posts (p columns NULL).
-- FROM users u, LATERAL (...) AS p drops users who have no posts.
```

WHY : the planner can execute one index-range scan per outer row (Nested Loop + Index Scan on `(user_id, created_at DESC)`), reading only N inner rows per parent. The window-function form must scan every inner row, compute a rank, then filter ; orders of magnitude more I/O for the same answer. Source : postgresql.org/docs/17/queries-table-expressions.html (LATERAL Subqueries), real-world benchmarks documented across pg-hackers list.

### Pattern 2 : Expand JSON array per row

ALWAYS use LATERAL with `jsonb_array_elements` (or `jsonb_array_elements_text`) to flatten a JSON array column to rows.
NEVER write a CASE / generate_series-by-array_length hack : the LATERAL form is idiomatic, planner-friendly, and version-stable.

```sql
-- event.payload contains {"items":[{"sku":"A","qty":2}, {"sku":"B","qty":5}]}
SELECT
  e.id        AS event_id,
  item->>'sku'         AS sku,
  (item->>'qty')::int  AS qty
FROM event e,
     LATERAL jsonb_array_elements(e.payload->'items') AS item;

-- Keep events whose payload has no items :
SELECT e.id, item->>'sku' AS sku
FROM event e
LEFT JOIN LATERAL jsonb_array_elements(e.payload->'items') AS item ON true;
```

WHY : `jsonb_array_elements` is a set-returning function ; called once per outer row, it emits one inner row per array element. Without LATERAL the function would not be allowed to reference `e.payload` (it does, because the keyword is optional for function calls, but writing LATERAL makes the cross-reference explicit and unambiguous). Source : postgresql.org/docs/17/functions-json.html (jsonb_array_elements), postgresql.org/docs/17/queries-table-expressions.html.

### Pattern 3 : Per-row `generate_series` (time bucketing, gap-fill)

ALWAYS use `LATERAL generate_series(...)` when you need a row-per-day/hour over a column range that varies per outer row.
NEVER hand-roll a recursive CTE for date ranges that vary per row : `generate_series` is faster and clearer.

```sql
-- One row per day in each subscription's active range :
SELECT
  s.id      AS subscription_id,
  d::date   AS active_day
FROM subscription s,
     LATERAL generate_series(s.started_at::date,
                             s.ended_at::date,
                             '1 day'::interval) AS d;

-- Keep subscriptions where started_at = ended_at + 1 day (no active days) :
SELECT s.id, d::date
FROM subscription s
LEFT JOIN LATERAL generate_series(s.started_at::date,
                                  s.ended_at::date,
                                  '1 day'::interval) AS d ON true;
```

WHY : `generate_series` arguments can reference outer columns. Without LATERAL, this would have raised an "invalid reference to FROM-clause entry" error in pre-9.3 PostgreSQL ; since v9.3 the keyword is optional for function calls, but writing it is good style. Source : postgresql.org/docs/17/functions-srf.html (generate_series), postgresql.org/docs/17/queries-table-expressions.html.

### Pattern 4 : `LEFT JOIN LATERAL ... ON true` to find "rows without children"

ALWAYS use the `LEFT JOIN LATERAL` pattern to find rows whose related-side subquery returns nothing.
NEVER use `NOT EXISTS` when you also need columns from the related side (in this case it would yield nothing) ; NEVER use `LEFT JOIN ... ON ... WHERE related.id IS NULL` when the LATERAL form is clearer.

```sql
-- Manufacturers with no products. The canonical postgres-docs example :
SELECT m.name
FROM manufacturers m
LEFT JOIN LATERAL get_product_names(m.id) AS pname ON true
WHERE pname IS NULL;

-- Equivalent NOT EXISTS form (when no related columns needed) :
SELECT m.name
FROM manufacturers m
WHERE NOT EXISTS (SELECT 1 FROM products p WHERE p.manufacturer_id = m.id);

-- LEFT JOIN LATERAL wins when you ALSO need columns from the related side :
-- "every customer + their MOST RECENT order if one exists" :
SELECT c.id, c.email, o.id AS last_order_id, o.ordered_at
FROM customer c
LEFT JOIN LATERAL (
  SELECT id, ordered_at
  FROM sales_order
  WHERE sales_order.customer_id = c.id
  ORDER BY ordered_at DESC
  LIMIT 1
) AS o ON true;
-- Customers without orders : c.* + o.* NULL.
```

WHY : `ON true` means "the join always succeeds" : the role of the LEFT JOIN is purely to preserve the outer row, not to evaluate a predicate. The actual filter logic lives inside the LATERAL subquery. Source : postgresql.org/docs/17/queries-table-expressions.html ("It is often particularly handy to LEFT JOIN to a LATERAL subquery, so that source rows will appear in the result even if the LATERAL subquery produces no rows for them.").

### Pattern 5 : `LATERAL` to call set-returning function with outer-row arguments

ALWAYS use LATERAL when calling a set-returning function whose arguments depend on a preceding FROM item.

```sql
-- Documented canonical example : pairs of polygons with close vertices
SELECT p1.id, p2.id, v1, v2
FROM polygons p1, polygons p2,
     LATERAL vertices(p1.poly) v1,
     LATERAL vertices(p2.poly) v2
WHERE (v1 <-> v2) < 10 AND p1.id != p2.id;

-- unnest of an array column per row
SELECT t.id, tag
FROM tagged_thing t,
     LATERAL unnest(t.tags) AS tag;
```

WHY : the function is invoked per outer row, with that row's value substituted into the argument. Without LATERAL the parser tolerates this for functions (optional keyword) but writing it is good style. Source : postgresql.org/docs/17/queries-table-expressions.html (Table Function `LATERAL` example).

### Pattern 6 : Distinguish LATERAL from a correlated subquery

ALWAYS reach for LATERAL when the subquery needs to return multiple rows or multiple columns ; reach for a correlated scalar subquery when the result is a single value.

```sql
-- Correlated SCALAR subquery (no LATERAL) : single value per outer row
SELECT c.id, c.email,
       (SELECT count(*) FROM sales_order o WHERE o.customer_id = c.id) AS order_count,
       (SELECT max(ordered_at) FROM sales_order o WHERE o.customer_id = c.id) AS last_order
FROM customer c;

-- LATERAL : multiple columns OR multiple rows per outer row
SELECT c.id, c.email, agg.order_count, agg.last_order
FROM customer c,
     LATERAL (
       SELECT count(*)             AS order_count,
              max(ordered_at)      AS last_order
       FROM sales_order o
       WHERE o.customer_id = c.id
     ) AS agg;
-- ONE LATERAL evaluation replaces TWO correlated subqueries.
-- For multiple ROWS (top-N) : correlated subquery cannot do it ; LATERAL is mandatory.
```

WHY : the planner can sometimes "pull up" both forms into a Nested Loop with the same physical execution, but the LATERAL form lets you return multiple columns in one pass. Source : postgresql.org/docs/17/queries-subqueries.html (Subqueries), postgresql.org/docs/17/queries-table-expressions.html (LATERAL).

## Anti-Patterns :

(One-liners ; full diagnosis + fix in `references/anti-patterns.md`.)

- `FROM a, LATERAL (...) sub` when outer rows must survive empty subquery : drops outer rows silently. Fix : `LEFT JOIN LATERAL (...) ON true`.
- Window-function top-N (`row_number() OVER (PARTITION BY ...) ≤ N`) on tables with a `(parent_id, order_col)` index : slower than LATERAL+LIMIT by orders of magnitude. Fix : LATERAL+LIMIT.
- LATERAL when a plain JOIN suffices : harder for the planner to optimize, no real benefit. Fix : plain `JOIN ... ON`.
- LATERAL subquery NOT referencing the outer row : the LATERAL keyword is a no-op ; same as a CROSS JOIN with the subquery's standalone result. Fix : drop LATERAL ; the planner handles the rest.
- Mixing LATERAL and the comma-join shorthand inconsistently : readers cannot tell which subquery is correlated. Fix : pick one style per query (explicit JOIN tree is clearer).
- LATERAL subquery with a `LIMIT 1` that returns the wrong row because of missing `ORDER BY` : silent picking of an arbitrary row. Fix : ALWAYS add a deterministic `ORDER BY` before `LIMIT`.
- Using LATERAL for aggregation across all rows (no outer row dependency) : LATERAL runs once per outer row, repeating the same work. Fix : a single GROUP BY query in a CTE or subquery.
- `LEFT JOIN LATERAL (...) ON predicate` where the predicate references inner columns : the predicate must be `true` for the LATERAL pattern to behave as outer-preserving. Fix : `ON true`, push filters inside the LATERAL.
- Missing alias on the LATERAL subquery : raises `42P01` syntax error. Fix : every LATERAL subquery needs an alias (`AS sub`).
- Calling a VOLATILE function inside LATERAL and reusing the alias across queries : different plan invocations may shuffle row order. Fix : add a deterministic order to the OUTER query.

## Reference Links :

- [references/methods.md](references/methods.md) : LATERAL syntax forms, keyword-required-vs-optional matrix, set-returning function compatibility, planner behavior notes.
- [references/examples.md](references/examples.md) : End-to-end examples with verified output : top-N-per-group, JSON array expansion, generate_series, set-returning functions, LEFT JOIN LATERAL idiom.
- [references/anti-patterns.md](references/anti-patterns.md) : Each anti-pattern with symptom, detection, and fix.

## See Also :

- `postgres-syntax-window-functions` : `row_number() OVER (PARTITION BY ...)` alternative to top-N-per-group when the index does not exist
- `postgres-syntax-jsonb` : `jsonb_array_elements`, JSON_TABLE (v17+) for projecting JSON arrays
- `postgres-syntax-cte-recursive` : recursive CTEs for hierarchical traversal (different problem class)
- `postgres-core-indexing-strategy` : the `(parent_id, order_col)` index that makes LATERAL+LIMIT fast
- `postgres-impl-explain-analyze` : verifying that the planner executes LATERAL as Nested Loop + Index Scan
- Vooronderzoek section : §5 (Query Features : Windows, CTE, LATERAL)
- Official docs : https://www.postgresql.org/docs/17/queries-table-expressions.html (LATERAL Subqueries section)
