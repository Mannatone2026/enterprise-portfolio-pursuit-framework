# Reference : Query Review Checklist + Report Schema

The deterministic review surface for `postgres-agents-query-reviewer`. Mechanisms
verified against PostgreSQL 17 docs and the package vooronderzoek (§4 Indexing,
§6 Performance, §19 Anti-Patterns). Last verified : 2026-05-19.

## The 13-point checklist

| ID | Trigger pattern | Severity | Rule | Cite skill |
|----|-----------------|----------|------|------------|
| C-1 | `SELECT *` in shipped code | WARNING | Enumerate columns | postgres-syntax-dml-ddl |
| C-2 | `UPDATE`/`DELETE` with no `WHERE` | CRITICAL | Add `WHERE` or confirm intent | postgres-syntax-dml-ddl |
| C-3 | Indexed column compared to a different-type literal | WARNING | Cast the literal, not the column | postgres-impl-indexing-strategy |
| C-4 | `jsonb @>` / `?` / `?|` / `?&` with no GIN index | WARNING | Add a GIN index | postgres-syntax-jsonb |
| C-5 | `f(indexed_col)` in `WHERE` with no expression index | WARNING | Expression index or sargable rewrite | postgres-impl-indexing-strategy |
| C-6 | `LIKE '%term%'` / `LIKE '%term'` (leading wildcard) | WARNING | pg_trgm GIN or full-text search | postgres-syntax-full-text-search |
| C-7 | One query per row in an app loop (N+1) | WARNING | `JOIN`, `= ANY($array)`, or `LATERAL` | postgres-syntax-lateral-joins |
| C-8 | `LIMIT n OFFSET m` with large `m` | WARNING | Keyset / seek pagination | postgres-impl-query-performance-toolkit |
| C-9 | Window aggregate vs `GROUP BY` mismatch | INFO | Match construct to per-row need | postgres-syntax-window-functions |
| C-10 | `NOT IN (subquery)` over a nullable column | CRITICAL | Rewrite to `NOT EXISTS` | postgres-syntax-dml-ddl |
| C-11 | `ORDER BY` with no `LIMIT` on a large result | WARNING | Add `LIMIT` or drop the sort | postgres-impl-query-performance-toolkit |
| C-12 | Hot `WHERE`/`JOIN` column with no index | WARNING | Confirm with EXPLAIN, add index | postgres-impl-query-performance-toolkit |
| C-13 | `DISTINCT`/`GROUP BY` collapsing join fan-out | WARNING* | Fix the join, not the symptom | postgres-syntax-lateral-joins |

\* C-13 escalates to CRITICAL when the fan-out feeds a `SUM`/`COUNT`/`AVG`, because
the aggregate result is then numerically wrong.

## Per-check rationale

### C-1 SELECT * (vooronderzoek §19.3)
`SELECT *` breaks when columns are added or reordered (ORM model classes are
especially fragile), forces the planner to fetch every column including
TOAST-able ones, and prevents index-only scans. Anchored exception : ad-hoc psql
exploration, never shipped code.

### C-2 Missing WHERE
A `WHERE`-less `UPDATE` or `DELETE` is syntactically valid and mutates the whole
table. There is no second chance. Treat every such statement as a CRITICAL until
the author confirms a full-table change in writing.

### C-3 Implicit cast (vooronderzoek §19.8)
`WHERE varchar_col = 123` makes Postgres cast `varchar_col` to a comparable type
on every row, which the index on `varchar_col` cannot serve. Casting the literal
(`= '123'`) keeps the column bare and sargable.

### C-4 JSONB without GIN (vooronderzoek §19.15)
`WHERE jsonb_col @> '{"k":"v"}'` is a Seq Scan without a GIN index. Use
`jsonb_path_ops` (smaller, faster) when only `@>` is needed; use default
`jsonb_ops` when the key-exists operators `?`, `?|`, `?&` are also used.

### C-5 Function on indexed column (vooronderzoek §4, §19.7)
A B-tree on `col` indexes `col`, not `lower(col)`. An expression index
`CREATE INDEX ON t ((lower(col)))` requires an IMMUTABLE function and needs
`ANALYZE` afterwards to gain statistics.

### C-6 Leading-wildcard LIKE (vooronderzoek §4)
A B-tree serves only anchored prefixes : `LIKE 'term%'`, `~ '^anchor'`. A leading
`%` makes the index unusable. Substring search needs a `pg_trgm` GIN index; word
search needs full-text search (`tsvector @@ tsquery`).

### C-7 N+1 (vooronderzoek §5 LATERAL)
N+1 is N network round-trips plus N plan executions where one set-based query
would do. Replace with a `JOIN`, an `= ANY($array)` lookup, or `LATERAL` for
top-N-per-group.

### C-8 OFFSET pagination
`OFFSET m` still reads and discards the first `m` rows every time, so page 1000
is far slower than page 1. Keyset pagination carries the last row's sort key :
`WHERE (created_at, id) > ($1, $2) ORDER BY created_at, id LIMIT n`.

### C-9 Window vs GROUP BY (vooronderzoek §19.16)
`SUM(x) OVER (PARTITION BY g)` keeps every input row; if the consumer only needs
the per-group total, `GROUP BY g` produces fewer rows and is cheaper. Conversely,
a self-join to compute a running total is clearer and cheaper as a window
function. INFO severity : both are correct, the choice is about cost and clarity.

### C-10 NOT IN with NULLs
`x NOT IN (SELECT y FROM t)` uses three-valued logic. If any `y` is NULL, the
predicate is never TRUE for any `x`, so the query silently returns zero rows.
`NOT EXISTS (SELECT 1 FROM t WHERE t.y = x)` is NULL-safe and usually plans
better (anti-join).

### C-11 ORDER BY without LIMIT
A large sorted result spills to disk (`Sort Method: external merge  Disk: ...`
in `EXPLAIN ANALYZE`) when the consumer reads only the head. Add a `LIMIT` so the
planner can use a `top-N heapsort`, or drop the `ORDER BY` if order is unused.

### C-12 Missing index (vooronderzoek §4, §19.2)
A frequently filtered column with no index seqscans on every execution. Foreign-key
child columns are a classic miss : without an index every parent DELETE/UPDATE
seqscans the child. Confirm with EXPLAIN before recommending an index.

### C-13 Fan-out masked by DISTINCT
A join on a non-unique key multiplies rows. `DISTINCT` removes the duplicate rows
and hides the bug, but any aggregate over the fanned-out set is wrong. Fix the
join cardinality (correct join key, or pre-aggregate in a subquery / `LATERAL`).

## Severity rubric

| Grade | Definition | Verdict effect |
|-------|------------|----------------|
| CRITICAL | Changes the result set : wrong rows, lost rows, data loss, wrong aggregate | Forces `REQUEST CHANGES` |
| WARNING | Correct result, but a seqscan-forcing / N+1 / unbounded-work defect that degrades at scale | Forces at most `APPROVE WITH WARNINGS` |
| INFO | Correct and fast enough; style or context-dependent choice | No verdict effect |

NEVER inflate a style nit to CRITICAL. NEVER downgrade a silent correctness bug
(C-2, C-10, C-13-escalated) to INFO.

## When to require EXPLAIN

Index-related findings (C-3, C-5, C-6, C-12, and any "this query is slow" claim)
are SUSPECTED until an execution plan confirms them. Recommend :

```sql
EXPLAIN (ANALYZE, BUFFERS) <the query>;
```

Read for : `Seq Scan` on a large table, a high `Rows Removed by Filter`,
`Sort Method: external merge  Disk:` (spill), and `Buffers: shared read=` (cold
I/O). Static correctness checks (C-2, C-10, C-13) need no plan to flag.

## Output report schema

```
SQL Review : <query identifier or file:line>

[CRITICAL] <location>
  Problem : <what is wrong and why it matters>
  Fix     : <concrete, copy-pasteable change>
  Cite    : <sibling skill>

[WARNING] <location>
  Problem : ...
  Fix     : ...
  Cite    : ...

[INFO] <location>
  Problem : ...
  Fix     : ...

Verdict : <APPROVE | APPROVE WITH WARNINGS | REQUEST CHANGES>   (n CRITICAL, n WARNING, n INFO)
```

Verdict rule :
- any CRITICAL  -> `REQUEST CHANGES`
- else any WARNING -> `APPROVE WITH WARNINGS`
- else -> `APPROVE`

If a check needs an EXPLAIN plan that was not provided, still list the finding
and append `(confirm with EXPLAIN (ANALYZE, BUFFERS))` to its Fix line.
