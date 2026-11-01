# Anti-Patterns : LATERAL Joins

LATERAL-specific anti-patterns with symptom, cause, SQLSTATE (where applicable), detection, and fix. Sources are official PostgreSQL 17 docs.

---

## AP-1 : INNER LATERAL when outer rows must survive

**Symptom** : "My query suddenly returns fewer rows than I have parents." Reports show NULL gaps in lookup tables. Counts of the outer relation do not match the query's row count.

**Cause** : `FROM outer, LATERAL (...) sub` (and the equivalent `CROSS JOIN LATERAL` / `INNER JOIN LATERAL ... ON true`) drops outer rows when the LATERAL subquery returns zero rows. The author intended outer-preservation but reached for the wrong form.

**SQLSTATE** : not an error path : silent row loss.

**Fix pattern** :

```sql
-- Wrong
SELECT u.*, p.title
FROM users u, LATERAL (
  SELECT title FROM posts p2 WHERE p2.user_id = u.id ORDER BY created_at DESC LIMIT 1
) AS p;

-- Right
SELECT u.*, p.title
FROM users u
LEFT JOIN LATERAL (
  SELECT title FROM posts p2 WHERE p2.user_id = u.id ORDER BY created_at DESC LIMIT 1
) AS p ON true;
```

Detection : `count(*)` of outer table vs query result. If outer is supposed to be 1:N and you expected to see every outer row, mismatches are this bug.

Source : postgresql.org/docs/17/queries-table-expressions.html ("It is often particularly handy to LEFT JOIN to a LATERAL subquery").

---

## AP-2 : Window-function top-N when LATERAL+LIMIT is dramatically faster

**Symptom** : "Top 3 most recent posts per user" query reads every post in the table. `EXPLAIN ANALYZE` shows WindowAgg + Sort over the full inner relation. Query runs for seconds on a table that should be instant.

**Cause** : the `row_number() OVER (PARTITION BY x ORDER BY y) <= N` pattern forces the engine to scan every inner row, sort them, compute a rank, then filter. With an index on `(parent_id, order_col DESC)`, a LATERAL+LIMIT plan reads only N inner rows per parent.

**SQLSTATE** : not an error path : performance regression.

**Fix pattern** :

```sql
-- Slow (every-post scan + window rank)
WITH ranked AS (
  SELECT *, row_number() OVER (PARTITION BY user_id ORDER BY created_at DESC) AS rn
  FROM posts
)
SELECT u.*, r.* FROM users u JOIN ranked r ON r.user_id = u.id AND r.rn <= 3;

-- Fast (one index-range-scan per user)
CREATE INDEX IF NOT EXISTS posts_user_created_desc ON posts (user_id, created_at DESC);

SELECT u.*, p.*
FROM users u
LEFT JOIN LATERAL (
  SELECT * FROM posts p2 WHERE p2.user_id = u.id ORDER BY created_at DESC LIMIT 3
) AS p ON true;
```

When the window form is BETTER : when the inner table has no index on `(parent_id, order_col)` and partial scans would re-read shared pages anyway, or when N approaches the average rows-per-group (then both forms touch the same data).

Source : postgresql.org/docs/17/queries-table-expressions.html, postgresql.org/docs/17/indexes-multicolumn.html.

---

## AP-3 : LATERAL when a plain JOIN suffices

**Symptom** : LATERAL appears in queries that have no per-row outer-reference inside the inner subquery, OR the inner subquery's only reference to the outer is an equi-join predicate that the planner could push.

**Cause** : the keyword was reached for by habit. A regular `JOIN ... ON` would express the same query, be easier to read, and give the planner more freedom (it can pick Hash Join / Merge Join instead of being locked into Nested Loop).

**SQLSTATE** : not an error path : missed optimization.

**Fix pattern** :

```sql
-- Unnecessary LATERAL (no top-N, no LIMIT, no per-row computation)
SELECT a.*, b.name
FROM a, LATERAL (SELECT name FROM b WHERE b.a_id = a.id) AS bx;

-- Equivalent and cheaper
SELECT a.*, b.name
FROM a JOIN b ON b.a_id = a.id;
```

Keep LATERAL when ANY of these is true : `LIMIT` inside, set-returning function, multiple rows from inner side, or the inner subquery references the outer row in a way that cannot be expressed as an ON clause.

Source : postgresql.org/docs/17/queries-table-expressions.html.

---

## AP-4 : Missing alias on LATERAL subquery

**Symptom** : `42601 syntax_error : subquery in FROM must have an alias`.

**Cause** : every subquery in the FROM clause requires an alias. LATERAL is no exception.

**SQLSTATE** : `42601` (`syntax_error`) at parse time.

**Fix pattern** :

```sql
-- Wrong
SELECT * FROM a, LATERAL (SELECT * FROM b WHERE b.a_id = a.id);
-- ERROR: subquery in FROM must have an alias

-- Right
SELECT * FROM a, LATERAL (SELECT * FROM b WHERE b.a_id = a.id) AS sub;
```

Source : postgresql.org/docs/17/sql-select.html.

---

## AP-5 : LIMIT inside LATERAL without ORDER BY

**Symptom** : "the same query returns different rows each run." Top-1-per-group queries pick an inconsistent row. Reports fluctuate.

**Cause** : `LIMIT 1` without `ORDER BY` picks whichever row PostgreSQL happens to scan first, which is implementation-defined and depends on plan, page order, and concurrent updates.

**SQLSTATE** : not an error path : non-determinism.

**Fix pattern** : ALWAYS add a deterministic `ORDER BY` before `LIMIT`.

```sql
-- Wrong
SELECT u.*, p.*
FROM users u, LATERAL (SELECT * FROM posts WHERE posts.user_id = u.id LIMIT 1) AS p;

-- Right
SELECT u.*, p.*
FROM users u, LATERAL (
  SELECT * FROM posts WHERE posts.user_id = u.id ORDER BY created_at DESC LIMIT 1
) AS p;
```

If "latest by created_at" is ambiguous (ties), add a tiebreaker : `ORDER BY created_at DESC, id DESC LIMIT 1`.

Source : postgresql.org/docs/17/sql-select.html (LIMIT clause), postgresql.org/docs/17/queries-order.html.

---

## AP-6 : `LEFT JOIN LATERAL ... ON predicate` (predicate references inner side)

**Symptom** : outer-preservation does not work as expected. Rows still drop. The LATERAL pattern feels broken.

**Cause** : the ON clause of a LEFT JOIN LATERAL should be `true`. Predicates that reference inner columns belong INSIDE the LATERAL subquery's WHERE clause. Putting them in the ON clause turns the join into a conditional outer join with surprising semantics.

**SQLSTATE** : not an error path : confusing result set.

**Fix pattern** :

```sql
-- Confusing (do not do this)
SELECT u.*, p.*
FROM users u
LEFT JOIN LATERAL (SELECT * FROM posts WHERE posts.user_id = u.id) AS p
  ON p.published = true;

-- Clear : push filters inside, keep ON true
SELECT u.*, p.*
FROM users u
LEFT JOIN LATERAL (
  SELECT * FROM posts
  WHERE posts.user_id = u.id AND posts.published = true
) AS p ON true;
```

Source : postgresql.org/docs/17/queries-table-expressions.html.

---

## AP-7 : LATERAL on uncorrelated subquery (LATERAL no-op)

**Symptom** : the LATERAL keyword appears but the subquery has no reference to outer FROM items. The query works but the keyword is misleading.

**Cause** : copy-paste from a template, or the cross-reference was removed during refactoring.

**SQLSTATE** : not an error path : style debt.

**Fix pattern** : drop LATERAL.

```sql
-- LATERAL is doing nothing here (subquery is independent)
SELECT a.*, b.*
FROM a, LATERAL (SELECT 1 AS one, 2 AS two) AS b;

-- Same query, clearer
SELECT a.*, b.*
FROM a, (SELECT 1 AS one, 2 AS two) AS b;

-- Or, often what was meant :
SELECT a.*, b.*
FROM a CROSS JOIN (VALUES (1, 2)) AS b(one, two);
```

Source : postgresql.org/docs/17/queries-table-expressions.html.

---

## AP-8 : LATERAL for aggregation across all rows

**Symptom** : a single global aggregate (`count(*) FROM big_table`) is computed inside a LATERAL that has no per-row outer reference, repeated once per outer row.

**Cause** : the LATERAL keyword does not magically cache results across outer rows. Each call re-evaluates the subquery (unless v14+ Memoize kicks in, which it will not for unparameterized aggregates).

**SQLSTATE** : not an error path : massive performance regression.

**Fix pattern** : compute the global aggregate once.

```sql
-- Wrong (computed once per outer row)
SELECT a.*, agg.total
FROM a, LATERAL (SELECT count(*) AS total FROM b) AS agg;

-- Right (computed once)
WITH agg AS (SELECT count(*) AS total FROM b)
SELECT a.*, agg.total FROM a, agg;

-- Or, cross-join with a scalar :
SELECT a.*, (SELECT count(*) FROM b) AS total FROM a;
```

Source : postgresql.org/docs/17/queries-with.html, postgresql.org/docs/17/runtime-config-query.html.

---

## AP-9 : LATERAL references item to its right

**Symptom** : `42P10 invalid_column_reference` ; "there is no FROM clause entry for table X" or similar.

**Cause** : LATERAL items can only reference items to their LEFT in the FROM list (or items on the left side of the JOIN they are inside). Items to the right are not yet in scope.

**SQLSTATE** : `42P10` (`invalid_column_reference`) at planning time.

**Fix pattern** : reorder the FROM clause so the cross-referenced item comes first.

```sql
-- Wrong (LATERAL references b, but b is to the right)
SELECT *
FROM LATERAL (SELECT * FROM x WHERE x.b_id = b.id) AS s,
     b;
-- ERROR : invalid reference to FROM-clause entry for table "b"

-- Right
SELECT *
FROM b,
     LATERAL (SELECT * FROM x WHERE x.b_id = b.id) AS s;
```

Source : postgresql.org/docs/17/queries-table-expressions.html ("for each row of the FROM item providing the cross-referenced column(s)").

---

## AP-10 : Mixing comma-LATERAL and JOIN-LATERAL in one query

**Symptom** : reviewers cannot tell which subquery is correlated to which outer item. Future refactors introduce subtle bugs because the precedence is hard to follow.

**Cause** : two different syntactic shorthands in the same FROM clause. PostgreSQL accepts the mix, but it is hostile to readers.

**SQLSTATE** : not an error path : maintenance hazard.

**Fix pattern** : pick one form per query. The explicit JOIN-tree form is generally clearer.

```sql
-- Confusing
SELECT *
FROM a, LATERAL (SELECT ... FROM b WHERE b.a_id = a.id) bs
  JOIN c ON c.id = bs.c_id;

-- Clearer (explicit JOIN tree throughout)
SELECT *
FROM a
CROSS JOIN LATERAL (SELECT ... FROM b WHERE b.a_id = a.id) AS bs
JOIN c ON c.id = bs.c_id;
```

Source : postgresql.org/docs/17/sql-select.html (FROM clause grammar).
