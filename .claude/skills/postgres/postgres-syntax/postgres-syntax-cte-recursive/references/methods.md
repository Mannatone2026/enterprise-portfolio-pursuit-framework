# CTE Methods Reference

Full syntax reference for Common Table Expressions in PostgreSQL 15, 16, and 17. Verified against `postgresql.org/docs/17/queries-with.html` and `postgresql.org/docs/17/sql-select.html` on 2026-05-19.

## Top-Level WITH Grammar :

```sql
[ WITH [ RECURSIVE ] cte_definition [, cte_definition ...] ]
SELECT | INSERT | UPDATE | DELETE | MERGE ...
;
```

Where `cte_definition` is :

```sql
cte_name [ ( column_alias [, column_alias ...] ) ] AS
  [ [ NOT ] MATERIALIZED ]
  ( SELECT | INSERT | UPDATE | DELETE | VALUES ... [ RETURNING ... ] )
  [ SEARCH { BREADTH | DEPTH } FIRST BY col [, col ...] SET search_seq_col_name ]
  [ CYCLE col [, col ...] SET cycle_mark_col [ TO 'cycle_value' DEFAULT 'noncycle_value' ] USING path_col_name ]
```

Notes :

- The `WITH RECURSIVE` keyword applies to the entire WITH list; even a single recursive CTE in a list requires `WITH RECURSIVE`.
- One `WITH RECURSIVE` can contain a mix of recursive and non-recursive CTEs.
- A CTE definition can reference any CTE defined EARLIER in the same WITH list (no forward references).
- CTEs are visible only within the statement they belong to (not across statements).

## MATERIALIZED Modifier :

| Modifier              | When applied                                   | Effect |
|-----------------------|------------------------------------------------|--------|
| (omitted, default)    | non-recursive CTE                              | v12+: inlined when referenced once and side-effect-free, else materialized. |
| `MATERIALIZED`        | non-recursive CTE                              | Always materialized (optimization fence). Planner cannot push predicates in or columns out. |
| `NOT MATERIALIZED`    | non-recursive CTE                              | Always inlined (substituted into the outer query). Planner can push predicates and prune columns. |
| (none accepted)       | recursive CTE                                  | RECURSIVE CTEs are always materialized; the keyword is silently ignored on `WITH RECURSIVE` definitions. |

When to force `MATERIALIZED` :

- The CTE contains a VOLATILE function whose result you want to compute ONCE.
- The CTE locks rows via `FOR UPDATE` and the lock must apply once.
- You want a deliberate optimization fence (rare; usually a planner workaround).
- The CTE is referenced multiple times but the planner inexplicably re-plans it (verify with EXPLAIN).

When to force `NOT MATERIALIZED` :

- You want predicate pushdown into the CTE for a single-reference case but the planner didn't pick it up.
- The CTE is cheap and inlining produces a better plan (verify with EXPLAIN).

## Recursive Branch Restrictions :

Inside the recursive (post-UNION) part of a `WITH RECURSIVE`, the following are NOT allowed :

| Restriction | Error |
|---|---|
| Reference the CTE name more than once | `42P19 recursive reference to query "..." must not appear more than once` |
| Aggregate function across the recursive reference | `42P19 aggregate functions are not allowed in WITH RECURSIVE` |
| `ORDER BY`, `LIMIT`, `OFFSET` in the recursive branch | parses, but the clause applies to the WHOLE union, not the iteration |
| `FOR UPDATE`, `FOR SHARE`, `FOR NO KEY UPDATE`, `FOR KEY SHARE` | `0A000 FOR UPDATE/FOR SHARE is not allowed with recursive references` |
| Outer joins where the recursive reference is on the nullable side | semantically reasonable but produces ambiguous results, raises an error |
| MERGE statement | `0A000 MERGE cannot be used in a WITH clause` (any context) |

The anchor (pre-UNION) branch has no such restrictions; it's a regular SELECT.

## UNION ALL vs UNION In Recursion :

- `UNION ALL` : keep duplicates. Per-iteration result is appended without dedup. Fast.
- `UNION` : dedup against the FULL running result (not just the current iteration). Per-iteration sort/hash. Slower, but guarantees row uniqueness across the whole CTE output.

Most recursive CTEs use UNION ALL and handle uniqueness via the data model (no duplicate parents in a tree) or explicit termination conditions.

## SEARCH Clause (v14+) :

```sql
SEARCH BREADTH FIRST BY col1 [, col2 ...] SET search_seq_col
SEARCH DEPTH   FIRST BY col1 [, col2 ...] SET search_seq_col
```

- Adds a column named `search_seq_col` to the CTE output, of type `varchar` (depth) or `bigint` (breadth) per implementation.
- Use it in the outer `ORDER BY` to materialize the traversal order.
- The `BY` columns identify the "node" for the traversal (typically the primary key of the row).

Internally, SEARCH BREADTH FIRST stores a sequence number per row; SEARCH DEPTH FIRST stores an array-of-tuples that sorts in depth-first order.

## CYCLE Clause (v14+) :

```sql
CYCLE col1 [, col2 ...] SET cycle_mark_col [ TO 'cycle_value' DEFAULT 'noncycle_value' ] USING path_col
```

- Tracks the path of visited `col1, col2, ...` tuples in `path_col` (array of records).
- Sets `cycle_mark_col` to `cycle_value` (default `'t'`/`true`) when a path revisits a tuple.
- Once detected, the recursion does NOT extend that branch further; the row marking the cycle IS still emitted (so you can detect it in the outer query).
- `cycle_mark_col` is typically used in the outer WHERE: `WHERE NOT cycle_mark_col`.

Combining SEARCH and CYCLE :

```sql
... ) SEARCH BREADTH FIRST BY id SET ord
      CYCLE id SET is_cycle USING path
SELECT * FROM cte ORDER BY ord;
```

Both clauses can co-exist; cycle detection runs FIRST, then SEARCH ordering is applied to the surviving rows.

## Data-Modifying CTE Snapshot Rules :

When INSERT / UPDATE / DELETE appears in a WITH clause :

1. ALL sub-statements observe the SAME snapshot (as of the start of the outer statement).
2. The effects of one CTE's writes are NOT visible to other CTEs in the same WITH.
3. The order in which sub-statements execute is NOT defined; do not assume "the DELETE runs before the INSERT". They appear to run simultaneously.
4. Triggers fire for each sub-statement; `RETURNING` reflects the post-trigger rows.
5. `RETURNING *` is the standard surface for downstream CTEs to consume.
6. Data-modifying CTEs must be at the TOP LEVEL of the WITH clause (you cannot nest a data-modifying CTE inside another CTE's body).

Implications :

- A query like `WITH d AS (DELETE FROM t WHERE old RETURNING *) INSERT INTO t SELECT FROM d` runs atomically in one snapshot but the INSERT will succeed only because the snapshot allowed the rows; concurrent transactions see one of two atomic states.
- Use `WITH d AS (DELETE ... RETURNING *) INSERT INTO archive SELECT * FROM d` to atomically move rows out.

## Catalogs And Inspection :

CTEs do not appear in any persistent catalog (they exist only during query execution). To inspect a CTE's plan :

```sql
EXPLAIN (ANALYZE, VERBOSE, BUFFERS)
WITH a AS (SELECT ...) SELECT * FROM a;
```

Look for "CTE Scan on a" (materialized) or absence (inlined). v12+ : an inlined CTE shows the subquery directly without a CTE node.

## Multiple CTEs In One WITH :

```sql
WITH
  active AS (SELECT id FROM users WHERE is_active),
  recent AS (SELECT user_id FROM orders WHERE created > now() - interval '7 days'),
  both AS (SELECT a.id FROM active a JOIN recent r ON a.id = r.user_id)
SELECT count(*) FROM both;
```

- Earlier CTEs are visible to later ones in the same WITH list.
- A later CTE can reference an earlier one multiple times.
- Each CTE is independently MATERIALIZED-or-not per the rules above.
