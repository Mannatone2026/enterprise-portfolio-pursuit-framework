---
name: postgres-agents-query-reviewer
description: >
  Use when reviewing SQL queries for correctness and performance before they ship, or auditing existing queries in a codebase.
  Prevents rubber-stamping queries without checking index usage, missing a WHERE-less UPDATE/DELETE, and overlooking implicit casts or un-indexed JSONB and LIKE patterns that force seqscans.
  Covers a deterministic SQL review checklist (SELECT *, missing WHERE, implicit casts, JSONB GIN gaps, leading-wildcard LIKE, N+1, OFFSET pagination, NOT IN with NULLs, fan-out), severity grading, and which sibling skill to cite per finding.
  Keywords: SQL review, query review, code review, query reviewer, SELECT star, missing WHERE, N+1 query, implicit cast, seqscan, slow query review, review my SQL, is this query good, audit queries, query checklist
license: MIT
compatibility: "Designed for Claude Code. Requires PostgreSQL 15, 16, or 17."
metadata:
  author: OpenAEC-Foundation
  version: "1.0"
---

# postgres-agents-query-reviewer

## Quick Reference :

This skill turns Claude into a SQL query reviewer. It is an orchestration skill, not a syntax reference: it defines a fixed 13-point review checklist, a severity grading scheme, and which sibling skill to cite for each finding. Run every check against every query. Never approve a query by inspection alone.

```
Review workflow :
  1. PARSE      -- identify statement type, target tables, WHERE/JOIN/ORDER, subqueries
  2. CHECK      -- run all 13 checks (C-1..C-13), record each hit
  3. EXPLAIN    -- for any index-related hit, recommend EXPLAIN (ANALYZE, BUFFERS)
  4. REPORT     -- emit structured findings + overall verdict
```

Severity grades :
- **CRITICAL** : a correctness bug or data-loss risk. Verdict cannot be APPROVE.
- **WARNING** : a performance defect that bites at scale (seqscan-forcing, N+1, OFFSET).
- **INFO** : style or context-dependent; the author decides.

Verdict : `APPROVE` (no CRITICAL, no WARNING) / `APPROVE WITH WARNINGS` (warnings only) / `REQUEST CHANGES` (any CRITICAL).

ALWAYS run all 13 checks. NEVER call a Seq Scan a defect without knowing the table size. NEVER review syntax only: an index-related finding is unconfirmed until an `EXPLAIN` plan is seen.

## When To Use This Skill :

ALWAYS use this skill when :
- A user asks "review this query", "is this SQL good", "why is this query slow", or pastes SQL for feedback
- Auditing existing queries in a codebase or a migration / PR diff
- Another agent produced SQL and it must be checked before it ships

NEVER use this skill for :
- Writing new SQL from scratch (use the relevant `postgres-syntax-*` skill)
- Reviewing schema DDL only, with no queries (use `postgres-agents-schema-reviewer`)
- Reviewing a migration's safety / locking (use `postgres-agents-migration-reviewer`)

## Decision Trees :

### Grading a finding :

```
A check fired. What severity?
  |
  |-- Does it change the RESULT SET (wrong rows, lost rows, data loss)?
  |     YES --> CRITICAL. Verdict becomes REQUEST CHANGES.
  |
  |-- Does it force a seqscan / extra round-trips / unbounded work AT SCALE?
  |     YES --> WARNING.
  |
  |-- Is it correct and fast enough, just not ideal style?
        YES --> INFO.
```

### Is this Seq Scan actually a problem? :

```
EXPLAIN shows a Seq Scan. Flag it?
  |
  |-- Table is small (a few thousand rows, fits in a page handful)?
  |     --> NOT a defect. A Seq Scan beats an index scan here. Do NOT flag.
  |
  |-- Query returns most of the table (low selectivity)?
  |     --> NOT a defect. A Seq Scan is the correct plan. Do NOT flag.
  |
  |-- Large table, selective predicate, "Rows Removed by Filter" is huge?
        --> WARNING. A usable index is missing or suppressed. Flag it.
```

### When to demand EXPLAIN before concluding :

```
Finding is about index usage (C-3, C-5, C-6, C-12, slow-query claims)?
  |-- YES --> Do NOT assert "this is slow". Recommend:
  |            EXPLAIN (ANALYZE, BUFFERS) <query>
  |           Report the finding as "suspected" until the plan confirms it.
  |-- NO  (C-2 missing WHERE, C-10 NOT IN NULL, C-13 fan-out) -->
            These are static correctness bugs. EXPLAIN not needed to flag them.
```

## The Review Checklist :

Run every check. Each check has a trigger, a rule, a severity, and a cite-skill. Full per-check rationale and examples are in references/methods.md and references/examples.md.

### C-1 : SELECT * in application code

ALWAYS flag `SELECT *` in code that ships. NEVER accept it outside ad-hoc psql.
Severity : WARNING. Cite : `postgres-syntax-dml-ddl`.
WHY : column drift breaks the app when columns are added or reordered, it over-fetches TOAST columns, and it blocks index-only scans. Fix : enumerate columns.

### C-2 : UPDATE / DELETE without WHERE

ALWAYS flag an `UPDATE` or `DELETE` with no `WHERE` clause. NEVER assume it is intentional.
Severity : CRITICAL. Cite : `postgres-syntax-dml-ddl`.
WHY : it mutates every row in the table. Fix : add the intended `WHERE`, or confirm in writing that a full-table change is the goal.

### C-3 : Implicit type cast in WHERE

ALWAYS flag a predicate that compares an indexed column to a literal of a different type (`varchar_col = 123`). NEVER cast the column.
Severity : WARNING. Cite : `postgres-impl-indexing-strategy`.
WHY : Postgres casts the column on every row, defeating the index. Fix : cast the literal instead (`varchar_col = '123'`).

### C-4 : JSONB @> / ? without a GIN index

ALWAYS flag `jsonb_col @> ...`, `?`, `?|`, `?&` when no GIN index covers the column.
Severity : WARNING. Cite : `postgres-syntax-jsonb`.
WHY : containment without a GIN index is a Seq Scan. Fix : `CREATE INDEX ... USING GIN (col jsonb_path_ops)` for `@>`-only workloads, plain `jsonb_ops` if key-exists operators are needed.

### C-5 : Function call on an indexed column in WHERE

ALWAYS flag `lower(col) = ...`, `date(col) = ...` etc. when no matching expression index exists.
Severity : WARNING. Cite : `postgres-impl-indexing-strategy`.
WHY : a plain B-tree on `col` cannot serve `f(col)`. Fix : `CREATE INDEX ON t ((lower(col)))` with an IMMUTABLE function, or rewrite the predicate to be sargable.

### C-6 : LIKE with a leading wildcard

ALWAYS flag `LIKE '%term%'` and `LIKE '%term'`. NEVER claim a B-tree index helps here.
Severity : WARNING. Cite : `postgres-syntax-full-text-search`.
WHY : a B-tree only serves anchored prefixes (`LIKE 'term%'`). Fix : a `pg_trgm` GIN index for substring search, or full-text search for word search.

### C-7 : N+1 query pattern

ALWAYS flag application code that issues one query per row in a loop.
Severity : WARNING. Cite : `postgres-syntax-lateral-joins`.
WHY : N round-trips where one set-based query suffices. Fix : a `JOIN`, `= ANY($array)`, or a `LATERAL` join for top-N-per-group.

### C-8 : OFFSET pagination with large offsets

ALWAYS flag `LIMIT n OFFSET m` paging where `m` grows large.
Severity : WARNING. Cite : `postgres-impl-query-performance-toolkit`.
WHY : `OFFSET m` still reads and discards `m` rows, so deep pages get linearly slower. Fix : keyset / seek pagination (`WHERE (sort_key) > ($last_key) ORDER BY sort_key LIMIT n`).

### C-9 : Window function vs GROUP BY mismatch

ALWAYS flag a window aggregate used where only a grouped total is needed, or a self-join used where a window function is clearer and cheaper.
Severity : INFO. Cite : `postgres-syntax-window-functions`.
WHY : `SUM(x) OVER (PARTITION BY g)` keeps every row; if per-row output is not used, `GROUP BY g` is cheaper. Fix : match the construct to whether per-row output is needed.

### C-10 : NOT IN (subquery) over a nullable column

ALWAYS flag `NOT IN (SELECT col FROM ...)` when `col` can be NULL.
Severity : CRITICAL. Cite : `postgres-syntax-dml-ddl`.
WHY : one NULL in the subquery makes `NOT IN` return zero rows for everything (three-valued logic). This is a silent correctness bug. Fix : `NOT EXISTS (SELECT 1 FROM ... WHERE ...)`.

### C-11 : ORDER BY without LIMIT on a large result

ALWAYS flag `ORDER BY` with no `LIMIT` when the result set is expected to be large.
Severity : WARNING. Cite : `postgres-impl-query-performance-toolkit`.
WHY : the full result must be sorted, often spilling to disk (`external merge`), when the consumer reads only the first rows. Fix : add a `LIMIT`, or drop the `ORDER BY` if the order is unused.

### C-12 : Missing index on a frequently-filtered column

ALWAYS flag a hot WHERE / JOIN column with no supporting index.
Severity : WARNING. Cite : `postgres-impl-query-performance-toolkit`.
WHY : every execution seqscans. Fix : recommend `EXPLAIN (ANALYZE, BUFFERS)` to confirm, then a B-tree (or partial / multicolumn) index sized to the predicate.

### C-13 : DISTINCT / GROUP BY masking a join fan-out bug

ALWAYS flag a `DISTINCT` or `GROUP BY` that exists only to collapse duplicate rows produced by a join.
Severity : WARNING (escalate to CRITICAL if it changes aggregate results). Cite : `postgres-syntax-lateral-joins`.
WHY : the duplicates mean the join multiplies rows; `DISTINCT` hides the symptom and any `SUM`/`COUNT` is then wrong. Fix : correct the join condition or aggregate in a subquery before joining.

## Output Format :

Emit findings as a list, then one verdict line. Per finding :

```
[SEVERITY] <location>
  Problem : <what is wrong>
  Fix     : <concrete change>
  Cite    : <sibling skill, if any>
```

End with exactly one verdict :

```
Verdict : REQUEST CHANGES   (1 CRITICAL, 2 WARNING, 0 INFO)
```

See references/methods.md for the full report schema and references/examples.md for complete worked reviews.

## Anti-Patterns :

(Reviewer anti-patterns. Full cause + symptom + fix in references/anti-patterns.md)

- Rubber-stamping : approving a query without running all 13 checks or seeing an `EXPLAIN`
- Seq-Scan-phobia : flagging every Seq Scan, including the correct plan on a small table
- Syntax-only review : checking SQL grammar but ignoring the execution plan and index usage
- Severity inflation : grading a style nit as CRITICAL, or a silent correctness bug as INFO
- Citing nothing : reporting a finding without the sibling skill that fixes it

## Reference Links :

- [references/methods.md](references/methods.md) : full checklist table, severity rubric, output report schema, EXPLAIN guidance
- [references/examples.md](references/examples.md) : complete worked review examples, before/after fixes per check, version-annotated
- [references/anti-patterns.md](references/anti-patterns.md) : reviewer anti-patterns with cause + symptom + fix

## See Also :

- postgres-agents-schema-reviewer : reviews table / column DDL design
- postgres-agents-migration-reviewer : reviews migration safety and locking
- postgres-impl-query-performance-toolkit : EXPLAIN reading, pg_stat_statements, slow-query hunting
- postgres-impl-indexing-strategy : index access methods, expression / partial indexes
- Vooronderzoek section : §4 (Indexing Strategy), §6 (Performance + EXPLAIN), §19 (Anti-Patterns)
- Official docs : https://www.postgresql.org/docs/17/using-explain.html
