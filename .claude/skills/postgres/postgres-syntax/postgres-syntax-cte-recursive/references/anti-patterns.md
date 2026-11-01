# CTE Anti-Patterns Reference

Ten documented bugs in CTE / WITH RECURSIVE / data-modifying CTE code with reproducible failure SQL and the official fix. Sources verified 2026-05-19 against `postgresql.org/docs/17/queries-with.html` and `postgresql.org/docs/release/12.0/`.

## 1. Pre-v12 Fence Reliance Broken By v12+ Inlining :

**Symptom** : an application upgraded from v11 to v12+ suddenly runs a VOLATILE function multiple times per row, returns different results across executions, or produces a much slower plan.

**Reproducer** :

```sql
WITH t AS (SELECT random() AS r FROM generate_series(1, 1000))
SELECT * FROM t WHERE r > 0.99 UNION ALL SELECT * FROM t WHERE r < 0.01;
-- Pre-v12 : random() evaluated ONCE, t materialized → both branches see same rows.
-- v12+ : t inlined → random() evaluated TWICE → different rows in each branch.
```

**Fix** :

```sql
WITH t AS MATERIALIZED (SELECT random() AS r FROM generate_series(1, 1000))
SELECT * FROM t WHERE r > 0.99 UNION ALL SELECT * FROM t WHERE r < 0.01;
```

`MATERIALIZED` restores the pre-v12 fence behavior. ALWAYS use it when a CTE contains a VOLATILE function whose result must be cached.

**Reference** : postgresql.org/docs/release/12.0/ § "Allow common table expressions (CTEs) to be inlined into the outer query".

## 2. Recursive CTE Without Termination :

**Symptom** : query never returns; session is killed by `idle_in_transaction_session_timeout` or runs OOM.

**Reproducer** :

```sql
WITH RECURSIVE counter AS (
  SELECT 1 AS n
  UNION ALL
  SELECT n + 1 FROM counter        -- no termination predicate
)
SELECT * FROM counter;
```

**Fix** : add a termination guard :

```sql
WITH RECURSIVE counter AS (
  SELECT 1 AS n
  UNION ALL
  SELECT n + 1 FROM counter WHERE n < 100
)
SELECT * FROM counter;
```

Recursive CTEs terminate when the recursive branch returns ZERO rows. Without a WHERE clause or self-limiting join condition, they loop forever.

## 3. Recursive CTE On A Cyclic Graph Without Cycle Detection :

**Symptom** : `out of memory`, session killed.

**Reproducer** :

```sql
INSERT INTO edges (src, dst) VALUES ('A', 'B'), ('B', 'A');   -- cycle

WITH RECURSIVE walk AS (
  SELECT src, dst FROM edges WHERE src = 'A'
  UNION ALL
  SELECT e.src, e.dst FROM edges e JOIN walk w ON e.src = w.dst
)
SELECT * FROM walk;
-- ERROR: out of memory
```

**Fix** (v14+) : use the CYCLE clause :

```sql
WITH RECURSIVE walk AS (
  SELECT src, dst FROM edges WHERE src = 'A'
  UNION ALL
  SELECT e.src, e.dst FROM edges e JOIN walk w ON e.src = w.dst
) CYCLE dst SET is_cycle USING path
SELECT * FROM walk WHERE NOT is_cycle;
```

**Fix** (v13 or older) : manual visited-set :

```sql
WITH RECURSIVE walk AS (
  SELECT src, dst, ARRAY[dst] AS visited FROM edges WHERE src = 'A'
  UNION ALL
  SELECT e.src, e.dst, w.visited || e.dst
  FROM edges e JOIN walk w ON e.src = w.dst
  WHERE NOT (e.dst = ANY(w.visited))
)
SELECT * FROM walk;
```

## 4. UNION Where UNION ALL Was Intended :

**Symptom** : recursion is 10-100x slower than expected, may even hang on deep trees. Per-iteration sort or hash for deduplication kills throughput.

**Reproducer** :

```sql
WITH RECURSIVE descendants AS (
  SELECT id, parent_id FROM nodes WHERE id = 1
  UNION                                            -- silently deduplicates ALL rows each iter
  SELECT n.id, n.parent_id FROM nodes n JOIN descendants d ON n.parent_id = d.id
)
SELECT count(*) FROM descendants;
```

**Fix** : use UNION ALL unless dedup is REQUIRED by the data semantics :

```sql
WITH RECURSIVE descendants AS (
  SELECT id, parent_id FROM nodes WHERE id = 1
  UNION ALL
  SELECT n.id, n.parent_id FROM nodes n JOIN descendants d ON n.parent_id = d.id
)
SELECT count(*) FROM descendants;
```

A well-formed tree (one parent per child) cannot produce duplicates via UNION ALL, so dedup is wasted work.

## 5. Aggregate Across The Recursive Reference :

**Symptom** :

```
ERROR:  aggregate functions are not allowed in WITH RECURSIVE
SQLSTATE 42P19
```

**Reproducer** :

```sql
WITH RECURSIVE cnt AS (
  SELECT 1 AS n
  UNION ALL
  SELECT count(*) FROM cnt WHERE n < 10              -- aggregate over CTE itself
)
SELECT * FROM cnt;
```

**Fix** : move the aggregation OUT of the recursive branch, into the outer query :

```sql
WITH RECURSIVE cnt AS (
  SELECT 1 AS n
  UNION ALL
  SELECT n + 1 FROM cnt WHERE n < 10
)
SELECT count(*) FROM cnt;       -- aggregate after the recursion completes
```

## 6. Multiple Self-References In Recursive Branch :

**Symptom** :

```
ERROR:  recursive reference to query "cte_name" must not appear more than once
SQLSTATE 42P19
```

**Reproducer** :

```sql
WITH RECURSIVE walk AS (
  SELECT id, parent_id FROM nodes WHERE id = 1
  UNION ALL
  SELECT a.id, a.parent_id FROM walk a JOIN walk b ON a.parent_id = b.id    -- twice
)
SELECT * FROM walk;
```

**Fix** : reference the CTE name exactly once; join other tables for the rest :

```sql
WITH RECURSIVE walk AS (
  SELECT id, parent_id FROM nodes WHERE id = 1
  UNION ALL
  SELECT n.id, n.parent_id FROM nodes n JOIN walk w ON n.parent_id = w.id
)
SELECT * FROM walk;
```

## 7. Expecting Cross-CTE Visibility In Data-Modifying WITH :

**Symptom** : "I did `DELETE` and then `SELECT` from the same table in a sibling CTE; the SELECT still saw the deleted rows."

**Reproducer** :

```sql
WITH
  d AS (DELETE FROM tasks WHERE done RETURNING *),
  s AS (SELECT * FROM tasks)               -- still sees pre-DELETE state
SELECT (SELECT count(*) FROM d) AS deleted, (SELECT count(*) FROM s) AS remaining;
-- 'remaining' includes the rows that 'd' will delete.
```

**Fix** : data-modifying CTEs all see the SAME snapshot. To observe the effect, use two separate statements :

```sql
WITH d AS (DELETE FROM tasks WHERE done RETURNING *) SELECT count(*) FROM d;
SELECT count(*) FROM tasks;
```

Or restructure to consume the data-modifying CTE's RETURNING directly :

```sql
WITH d AS (DELETE FROM tasks WHERE done RETURNING *)
INSERT INTO done_archive SELECT * FROM d;
```

## 8. MERGE Inside WITH :

**Symptom** :

```
ERROR:  MERGE not supported in WITH query
SQLSTATE 0A000
```

**Reproducer** :

```sql
WITH m AS (
  MERGE INTO t USING src ON t.id = src.id
  WHEN MATCHED THEN UPDATE SET v = src.v
  RETURNING merge_action(), t.*
)
SELECT * FROM m;
```

**Fix** : run MERGE as the top-level statement. If you need to consume its RETURNING result, write to a temp table :

```sql
CREATE TEMP TABLE merge_out AS
MERGE INTO t USING src ON t.id = src.id
WHEN MATCHED THEN UPDATE SET v = src.v
RETURNING merge_action() AS action, t.*;
```

Status as of v17 : MERGE in WITH is on the TODO; not yet supported.

## 9. Forward Reference In WITH List :

**Symptom** :

```
ERROR:  relation "later_cte" does not exist
```

**Reproducer** :

```sql
WITH
  a AS (SELECT * FROM later_cte),       -- forward reference
  later_cte AS (SELECT 1)
SELECT * FROM a;
```

**Fix** : reorder so referenced CTEs are defined first :

```sql
WITH
  later_cte AS (SELECT 1),
  a AS (SELECT * FROM later_cte)
SELECT * FROM a;
```

WITH list is left-to-right; later definitions can reference earlier ones, never vice versa.

## 10. Forgetting `RECURSIVE` Keyword :

**Symptom** :

```
ERROR:  relation "cte_name" does not exist
```

**Reproducer** :

```sql
WITH walk AS (                              -- missing RECURSIVE
  SELECT id, parent_id FROM nodes WHERE id = 1
  UNION ALL
  SELECT n.id, n.parent_id FROM nodes n JOIN walk w ON n.parent_id = w.id
) SELECT * FROM walk;
-- ERROR: relation "walk" does not exist (the self-reference is invalid without RECURSIVE)
```

**Fix** : add `RECURSIVE` :

```sql
WITH RECURSIVE walk AS (
  SELECT id, parent_id FROM nodes WHERE id = 1
  UNION ALL
  SELECT n.id, n.parent_id FROM nodes n JOIN walk w ON n.parent_id = w.id
) SELECT * FROM walk;
```

The `RECURSIVE` keyword applies to the entire WITH list and is required as soon as ANY CTE in the list references itself.
