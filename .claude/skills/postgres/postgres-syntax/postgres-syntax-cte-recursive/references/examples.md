# CTE Examples Reference

Ten runnable patterns for CTEs on PostgreSQL 15, 16, and 17. Verified against `postgresql.org/docs/17/queries-with.html` on 2026-05-19.

## Example 1 : Non-Recursive CTE For Readability :

```sql
WITH recent_orders AS (
  SELECT * FROM orders WHERE created > now() - interval '30 days'
),
high_value AS (
  SELECT * FROM recent_orders WHERE amount > 1000
)
SELECT customer_id, count(*) AS big_orders
FROM high_value GROUP BY customer_id;
```

Each CTE references the previous one. v12+ inlines them when single-referenced and side-effect-free, so the plan is equivalent to a nested-subquery form.

## Example 2 : MATERIALIZED To Force Fence :

```sql
WITH expensive AS MATERIALIZED (
  SELECT id, slow_function(payload) AS result FROM jobs
)
SELECT a.*, b.*
FROM expensive a JOIN expensive b ON a.result = b.result;
```

`slow_function` is computed once per row, not per join probe. Without `MATERIALIZED` the planner may decide to inline and re-call the function.

## Example 3 : Tree Walk Down From A Root :

```sql
WITH RECURSIVE descendants AS (
  SELECT id, parent_id, name, 1 AS depth, ARRAY[id] AS path
  FROM categories WHERE id = 1                                 -- anchor : the root
  UNION ALL
  SELECT c.id, c.parent_id, c.name, d.depth + 1, d.path || c.id
  FROM categories c JOIN descendants d ON c.parent_id = d.id
)
SELECT id, name, depth, path FROM descendants ORDER BY path;
```

The `path` array lets the outer ORDER BY produce a depth-first traversal. `depth` lets you limit (`WHERE depth <= 5`) without a CYCLE clause.

## Example 4 : Tree Walk Up To A Root :

```sql
WITH RECURSIVE ancestors AS (
  SELECT id, parent_id, name FROM categories WHERE id = 999    -- anchor : the leaf
  UNION ALL
  SELECT c.id, c.parent_id, c.name
  FROM categories c JOIN ancestors a ON c.id = a.parent_id
)
SELECT * FROM ancestors;
```

Same structure; the JOIN condition reverses direction.

## Example 5 : Graph Traversal With CYCLE Clause (v14+) :

```sql
WITH RECURSIVE reachable AS (
  SELECT src, dst FROM edges WHERE src = 'A'                   -- anchor
  UNION ALL
  SELECT e.src, e.dst
  FROM edges e JOIN reachable r ON e.src = r.dst
) CYCLE dst SET is_cycle USING path
SELECT src, dst, path FROM reachable WHERE NOT is_cycle;
```

`is_cycle` is `true` on the row that closes a cycle; filtering it out prevents the cycle's revisit while still surfacing the path for debugging.

## Example 6 : Graph Traversal With Manual Visited-Set :

```sql
WITH RECURSIVE reachable AS (
  SELECT src, dst, ARRAY[dst] AS visited FROM edges WHERE src = 'A'
  UNION ALL
  SELECT e.src, e.dst, r.visited || e.dst
  FROM edges e JOIN reachable r ON e.src = r.dst
  WHERE NOT (e.dst = ANY(r.visited))                          -- skip already-visited
)
SELECT src, dst, visited FROM reachable;
```

Use this on v13 or earlier (CYCLE is v14+). On v14+ the CYCLE clause is shorter and the planner generates equivalent code.

## Example 7 : SEARCH BREADTH FIRST For Level-By-Level Output (v14+) :

```sql
WITH RECURSIVE walk AS (
  SELECT id, parent_id, name FROM nodes WHERE parent_id IS NULL
  UNION ALL
  SELECT n.id, n.parent_id, n.name FROM nodes n JOIN walk w ON n.parent_id = w.id
) SEARCH BREADTH FIRST BY id SET ord
SELECT id, name, ord FROM walk ORDER BY ord;
```

The `ord` column makes BFS deterministic. Without `SEARCH`, the order of rows is implementation-defined.

## Example 8 : Generate Number Series Via Recursion :

```sql
WITH RECURSIVE fib AS (
  SELECT 1 AS n, 0::bigint AS a, 1::bigint AS b
  UNION ALL
  SELECT n + 1, b, a + b FROM fib WHERE n < 30
)
SELECT n, a AS fibonacci FROM fib;
```

Useful when `generate_series` does not fit the pattern (e.g. relational dependency between iterations). For pure ranges, prefer `SELECT generate_series(1, 30)` (cheaper).

## Example 9 : Atomic Move-And-Archive (Data-Modifying CTE) :

```sql
WITH archived AS (
  DELETE FROM products WHERE discontinued_at < now() - interval '1 year'
  RETURNING *
)
INSERT INTO products_archive
SELECT *, now() AS archived_at FROM archived;
```

Both CTEs see the same snapshot. The DELETE removes the rows and surfaces them via RETURNING; the INSERT writes them into the archive. From outside the statement, the rows appear to teleport atomically.

## Example 10 : Update With Audit Log :

```sql
WITH updated AS (
  UPDATE accounts SET balance = balance + 100 WHERE id = ANY($1)
  RETURNING id, balance, balance - 100 AS old_balance
)
INSERT INTO account_audit (account_id, old_balance, new_balance, at)
SELECT id, old_balance, balance, now() FROM updated;
```

The UPDATE produces both old and new values via RETURNING expressions; the audit INSERT consumes them downstream. All in one statement, same snapshot, same transaction.

## Bonus : Recursive String Splitter (No string_to_table) :

```sql
WITH RECURSIVE split AS (
  SELECT 1 AS pos,
         strpos('a,b,c,d', ',') AS sep,
         substring('a,b,c,d' FROM 1 FOR strpos('a,b,c,d', ',') - 1) AS piece,
         substring('a,b,c,d' FROM strpos('a,b,c,d', ',') + 1) AS rest
  UNION ALL
  SELECT pos + 1,
         strpos(rest, ','),
         CASE WHEN strpos(rest, ',') = 0 THEN rest ELSE substring(rest FROM 1 FOR strpos(rest, ',') - 1) END,
         CASE WHEN strpos(rest, ',') = 0 THEN '' ELSE substring(rest FROM strpos(rest, ',') + 1) END
  FROM split WHERE rest <> ''
)
SELECT pos, piece FROM split;
```

For production code prefer `string_to_table('a,b,c,d', ',')` (v14+) or `regexp_split_to_table`. This example illustrates the recursive-CTE termination pattern (`WHERE rest <> ''`).
