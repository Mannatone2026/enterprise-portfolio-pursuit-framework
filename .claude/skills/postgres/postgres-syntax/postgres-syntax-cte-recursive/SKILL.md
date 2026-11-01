---
name: postgres-syntax-cte-recursive
description: >
  Use when writing readable multi-stage queries, traversing parent/child trees, or constructing data-modifying CTEs that return both before and after states.
  Prevents pre-v12 fence-reliance breaking under v12+ inlining (NOT MATERIALIZED default), infinite-loop recursive CTEs without termination, and reaching for stored procedures when a recursive CTE suffices.
  Covers non-recursive CTE, RECURSIVE CTE (UNION ALL vs UNION, termination, SEARCH/CYCLE v14+), MATERIALIZED vs NOT MATERIALIZED (v12+), data-modifying CTE (INSERT/UPDATE/DELETE in WITH), tree-walk + graph patterns.
  Keywords: CTE, WITH, WITH RECURSIVE, MATERIALIZED, NOT MATERIALIZED, SEARCH, CYCLE, data-modifying CTE, tree walk, graph traversal, recursive query, my recursive CTE never returns, query inlined unexpectedly, how to traverse parent_id, fence broken v12, ERROR aggregate functions are not allowed in WITH RECURSIVE, infinite loop CTE, out of memory recursive
license: MIT
compatibility: "Designed for Claude Code. Requires PostgreSQL 15, 16, or 17."
metadata:
  author: OpenAEC-Foundation
  version: "1.0"
---

# postgres-syntax-cte-recursive

## Quick Reference :

Common Table Expressions (`WITH name AS (...)`) factor a query into named, ordered, readable stages. Three flavors :

1. **Non-recursive CTE** : `WITH a AS (SELECT ...) SELECT ... FROM a`. Equivalent to a subquery in the FROM clause, but named and reusable.
2. **Recursive CTE** : `WITH RECURSIVE a AS (anchor UNION ALL recur)`. Used for tree-walks, graph traversal, generated sequences, and any "expand until done" computation.
3. **Data-modifying CTE** : `WITH a AS (INSERT/UPDATE/DELETE ... RETURNING *) SELECT FROM a`. Lets a single statement perform DML and surface the affected rows for downstream use, with all CTEs sharing one snapshot.

Two facts to internalize before writing any CTE :

- **v12 behavior change**: non-recursive CTEs are inlined by default (`NOT MATERIALIZED`) when referenced once and side-effect-free. Pre-v12 they were always materialized (optimization fence). If your code relies on the fence to prevent the planner from re-evaluating an expensive function or unsafe ordering, write `WITH x AS MATERIALIZED (...)` explicitly.
- **Recursive CTEs MUST terminate.** The recursive branch must produce zero rows eventually, or PostgreSQL loops until the work-table exhausts memory.

Minimum-viable non-recursive CTE :

```sql
WITH active_users AS (
  SELECT id, email FROM users WHERE last_login > now() - interval '30 days'
)
SELECT count(*) FROM active_users;
```

Minimum-viable recursive CTE (tree-walk down from a root) :

```sql
WITH RECURSIVE descendants AS (
  SELECT id, parent_id, 1 AS depth FROM nodes WHERE id = 42        -- anchor
  UNION ALL
  SELECT n.id, n.parent_id, d.depth + 1                              -- recursive
  FROM nodes n JOIN descendants d ON n.parent_id = d.id
)
SELECT * FROM descendants;
```

## When To Use This Skill :

ALWAYS use this skill when :

- Writing a query with intermediate stages that would be hard to read as nested subqueries
- Traversing a parent-child (tree) or many-to-many (graph) relation
- Combining INSERT/UPDATE/DELETE with a SELECT in one atomic statement
- Generating a sequence of values without `generate_series`
- Debugging "my CTE was supposed to be cached but the planner re-runs it" (v12+ inlining)
- Debugging "my recursive CTE never returns" (missing termination or cycle)

NEVER use this skill for :

- Window functions (see [postgres-syntax-window-functions](../postgres-syntax-window-functions/SKILL.md) if it exists)
- LATERAL joins (different mechanic, see [postgres-syntax-lateral](../postgres-syntax-lateral/SKILL.md))
- Materialized VIEWS (persistent stored result, different feature, see [postgres-impl-indexes](../../postgres-impl/) or core/architecture)
- MERGE inside the recursive branch (NOT supported; MERGE cannot appear in WITH RECURSIVE per `postgres-syntax-upsert-merge`)

## Decision Trees :

### Plain query, subquery, or CTE :

```
Is the intermediate result used MORE THAN ONCE in the outer query ?
├── YES → CTE (named, deduplicated reference). Consider MATERIALIZED if expensive.
└── NO  → Is the intermediate result complex enough to harm readability when nested ?
         ├── YES → CTE (purely for readability; inlined by default in v12+).
         └── NO  → inline subquery or join.
```

### MATERIALIZED or NOT MATERIALIZED :

```
Is the CTE referenced exactly once ?
├── YES → default (NOT MATERIALIZED) is correct. Planner can push predicates in, prune columns out.
└── NO  → Default is MATERIALIZED (multi-reference). Usually correct.

Is the CTE expensive (calls a VOLATILE function, large aggregation, expensive scan) ?
└── YES → force MATERIALIZED even if referenced once : avoids re-execution.

Does the CTE call a function with side effects (write, NOW() captured for the snapshot) ?
└── YES → force MATERIALIZED. Without the fence, the planner may inline and re-invoke the function.
```

### Recursive CTE termination strategy :

```
Does the data structure have known finite depth (e.g. file-system tree, org chart) ?
├── YES → recursion terminates when no row joins → recursive branch returns zero rows.
└── NO  → the data has cycles (graph). Choose ONE:
         ├── v14+ → use CYCLE clause : CYCLE id SET is_cycle USING path
         ├── manual visited-set : recur joins on NOT (id = ANY(path)), append to path
         └── depth guard : recur joins on depth < N to cap iterations
```

### UNION ALL or UNION :

```
Can the same row legitimately appear twice in the result ?
├── YES → UNION ALL (keep duplicates ; common for paths in graphs).
└── NO  → UNION (deduplicate ; required if rows must be unique).

Notes :
- UNION ALL is the default choice for recursion. Use UNION only when dedup is REQUIRED.
- UNION inside WITH RECURSIVE incurs a per-iteration sort/hash to deduplicate, which is slower.
```

## Core Syntax :

### Non-Recursive CTE :

```sql
WITH cte_name [ ( col1, col2, ... ) ] AS [ MATERIALIZED | NOT MATERIALIZED ] (
  SELECT ...
)
[ , another_cte AS ( ... ) ]
SELECT ... FROM cte_name [ JOIN another_cte ... ];
```

Notes :

- Optional column list after the CTE name renames the SELECT's output columns.
- Multiple CTEs separated by commas, in dependency order (a CTE can reference an earlier CTE in the same WITH).
- A CTE in the SAME WITH list cannot reference one defined later (no forward references).
- v12+ default : `NOT MATERIALIZED` when referenced once AND has no VOLATILE function AND has no DML.

### Recursive CTE :

```sql
WITH RECURSIVE cte_name [ ( col1, col2, ... ) ] AS (
  -- ANCHOR (non-recursive base)
  SELECT ...
  UNION ALL                                              -- or UNION (deduplicate)
  -- RECURSIVE BRANCH
  SELECT ... FROM cte_name JOIN ... WHERE ...
)
[ SEARCH { DEPTH | BREADTH } FIRST BY col1, col2 SET search_seq ]   -- v14+
[ CYCLE col1, col2 SET is_cycle [ TO 'Y' DEFAULT 'N' ] USING path ] -- v14+
SELECT ... FROM cte_name;
```

Execution :

- Anchor SELECT runs once → its rows form the initial working table.
- Recursive SELECT runs against the working table; the result becomes the new working table for the next iteration.
- Loop terminates when the recursive SELECT returns ZERO rows.
- Final result is the UNION ALL (or UNION) of all iterations.

Restrictions on the recursive branch :

- Must reference the CTE name exactly once (in v15-v17). Self-joining the CTE name twice raises `42P19`.
- Cannot contain aggregates that span the recursive reference (e.g. `SELECT count(*) FROM cte_name` in the recursive branch). Per-row aggregates from OTHER tables are fine.
- No `ORDER BY`, `LIMIT`, or `OFFSET` inside the recursive query (they apply to the outer SELECT instead).
- No `FOR UPDATE` / `FOR SHARE` inside.

### SEARCH and CYCLE Clauses (v14+) :

`SEARCH` adds an output column that imposes a traversal order :

```sql
WITH RECURSIVE walk AS (
  SELECT id, parent_id FROM nodes WHERE parent_id IS NULL
  UNION ALL
  SELECT n.id, n.parent_id FROM nodes n JOIN walk w ON n.parent_id = w.id
) SEARCH BREADTH FIRST BY id SET ord
SELECT id, parent_id, ord FROM walk ORDER BY ord;
```

- `SEARCH BREADTH FIRST BY col` : the `ord` column orders rows level-by-level (anchor first, then all children, then all grandchildren).
- `SEARCH DEPTH FIRST BY col` : the `ord` column orders rows by full path (anchor, its first child and that child's full subtree, then anchor's second child, etc).
- The SET column is exposed in the CTE's output for use in the outer `ORDER BY`.

`CYCLE` adds two output columns to detect and break cycles without a manual visited-set :

```sql
WITH RECURSIVE walk AS (
  SELECT id, target_id FROM edges WHERE id = 'A'
  UNION ALL
  SELECT e.id, e.target_id FROM edges e JOIN walk w ON e.id = w.target_id
) CYCLE id SET is_cycle USING path
SELECT id, target_id, is_cycle, path FROM walk WHERE NOT is_cycle;
```

- `CYCLE col1[, col2] SET flag [TO 'Y' DEFAULT 'N'] USING path_col` : `flag` is `'Y'` on the row where the cycle closes, `path_col` is an array of the visited (col1[,col2]) tuples up to that point.
- Once a cycle is detected for a path, the recursion does NOT extend that path further (prevents infinite loop).

### Data-Modifying CTE :

```sql
WITH archived AS (
  DELETE FROM products WHERE created < '2010-01-01' RETURNING *
)
INSERT INTO products_archive SELECT * FROM archived;
```

Rules :

- INSERT / UPDATE / DELETE / MERGE inside WITH must use `RETURNING` for downstream CTEs to see the rows.
- All sub-statements in a single WITH see the SAME snapshot. A CTE's writes are NOT visible to other CTEs in the same WITH.
- Side-effects all happen, but in an unspecified order. Do NOT rely on one CTE's UPDATE seeing another CTE's INSERT.
- Triggers fire as usual. RETURNING reflects post-trigger rows.
- Data-modifying CTEs must be at the TOP LEVEL of the WITH clause, not nested inside another CTE.

## Common Errors :

| SQLSTATE | Error | Cause | Fix |
|---|---|---|---|
| 42P19 | `recursive reference to query "..." must not appear within its non-recursive term` | Anchor branch references the CTE itself | Move the self-reference into the recursive (post-UNION) branch. |
| 42P19 | `recursive reference to query "..." must not appear more than once` | Recursive branch references the CTE name twice (e.g. self-join) | Refactor to reference it once; join other tables instead. |
| 42P19 | `aggregate functions are not allowed in WITH RECURSIVE` | Recursive branch contains an aggregate over the recursive CTE | Move aggregation to the outer query (after the CTE). |
| (out of memory) | session killed / OOM | Recursive CTE never terminates (cycle without break) | Add CYCLE clause (v14+) or manual visited-set + `NOT (id = ANY(path))`. |
| 42703 | column "cte_name.col" does not exist | Forward reference to a later CTE | Reorder so referenced CTE is defined first. |
| 0A000 | `MERGE cannot be used in a WITH clause` | Data-modifying CTE containing MERGE | Wrap MERGE in a separate statement, or use INSERT ... ON CONFLICT in the CTE instead. |

## Anti-Patterns :

The four most-common CTE bugs (full list with reproducers in references/anti-patterns.md) :

1. **Pre-v12 fence reliance** : code written assuming "the CTE is materialized so the planner cannot push the WHERE inside it" silently breaks on v12+ when inlining kicks in. Fix : `AS MATERIALIZED`.
2. **Recursive CTE without termination guard** on a graph : infinite loop until OOM. Fix : CYCLE clause (v14+) or visited-set with `NOT (id = ANY(path))`.
3. **`UNION` instead of `UNION ALL` by accident** in recursion : per-iteration deduplication kills performance, often hides logic bugs. Use UNION ALL unless dedup is required.
4. **Expecting one data-modifying CTE to see another's writes** : both see the same pre-statement snapshot. Use sequential statements if ordering matters.

## Version Notes :

| Feature | v15 | v16 | v17 |
|---|---|---|---|
| Non-recursive CTE | yes | yes | yes |
| MATERIALIZED / NOT MATERIALIZED keywords | yes (since v12) | yes | yes |
| Default NOT MATERIALIZED for single-ref CTE | yes (since v12) | yes | yes |
| WITH RECURSIVE | yes | yes | yes |
| Data-modifying CTE | yes | yes | yes |
| SEARCH BREADTH/DEPTH FIRST | yes (since v14) | yes | yes |
| CYCLE clause | yes (since v14) | yes | yes |
| MERGE inside WITH | no (planned, not yet) | no | no |
| MERGE in CTE source | no | no | no |

All four major behavioral pieces (inlining default, RECURSIVE, SEARCH/CYCLE, data-modifying CTE) are stable across v15-v17. See [postgres-core-version-matrix](../../postgres-core/postgres-core-version-matrix/SKILL.md) for the cross-package version reference.

## Reference Links :

- [methods.md](references/methods.md) : full `WITH` syntax, MATERIALIZED semantics, recursive-branch restrictions, SEARCH/CYCLE clauses, data-modifying CTE snapshot rules.
- [examples.md](references/examples.md) : 10 runnable patterns including tree-walk, graph cycle-break, generate-series-clone, atomic move-and-archive, before/after audit log.
- [anti-patterns.md](references/anti-patterns.md) : 10 documented bugs with reproducers and the official fix.

Official PostgreSQL documentation (verified 2026-05-19) :

- WITH Queries (Common Table Expressions) : https://www.postgresql.org/docs/17/queries-with.html
- SELECT (full grammar, includes WITH) : https://www.postgresql.org/docs/17/sql-select.html
- Release notes v12 (NOT MATERIALIZED default) : https://www.postgresql.org/docs/release/12.0/
- Release notes v14 (SEARCH and CYCLE clauses) : https://www.postgresql.org/docs/release/14.0/
