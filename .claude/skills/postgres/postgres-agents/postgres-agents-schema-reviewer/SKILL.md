---
name: postgres-agents-schema-reviewer
description: >
  Use when reviewing a PostgreSQL schema or DDL design before it ships, or auditing an existing schema for structural problems.
  Prevents shipping tables with no primary key, un-indexed foreign keys, multi-tenant tables with no RLS strategy, SERIAL where IDENTITY belongs, and timestamp instead of timestamptz for event times.
  Covers a deterministic schema review checklist (snake_case naming, IDENTITY vs SERIAL, missing PK, FK indexing, redundant indexes, RLS coverage, jsonb vs CSV-in-text, timestamptz, normalization), severity grading, cite-sibling-skill per finding, GOOD / NEEDS-WORK / POOR verdict.
  Keywords: schema review, DDL review, database design review, table design, naming convention, missing primary key, foreign key index, RLS coverage, normalization, IDENTITY vs serial, review my schema, is this schema good, audit database design, schema checklist
license: MIT
compatibility: "Designed for Claude Code. Requires PostgreSQL 15, 16, or 17."
metadata:
  author: OpenAEC-Foundation
  version: "1.0"
---

# postgres-agents-schema-reviewer

## Quick Reference :

This skill turns Claude into a schema design reviewer. It is an orchestration
skill, not a DDL syntax reference: it defines a fixed 16-point checklist, a
severity grading scheme, and which sibling skill to cite for each finding. Run
every check against every table. Never approve a schema by inspection alone.

```
Review workflow :
  1. PARSE   -- collect every CREATE TABLE, column, constraint, index;
               build the foreign-key graph across ALL tables first
  2. CHECK   -- run all 16 checks (S-1..S-16) against every table
  3. GRADE   -- assign each hit a severity; mark choices that may be deliberate
  4. REPORT  -- emit structured findings + one overall verdict
```

Severity grades :
- **CRITICAL** : a structural defect that corrupts data or breaks tenancy. Verdict cannot be GOOD.
- **WARNING** : a design flaw that bites at scale or in maintenance.
- **INFO** : style or context-dependent; the author decides.

Verdict : `GOOD` (no CRITICAL, no WARNING) / `NEEDS-WORK` (WARNING only) /
`POOR` (any CRITICAL).

ALWAYS build the FK graph before grading: an un-indexed FK or a missing-PK is
invisible when one table is read in isolation. NEVER hard-fail a deliberate
denormalization or a flagged choice without first asking the author for the
rationale. NEVER spend the review on cosmetic naming while a CRITICAL is unaddressed.

## When To Use This Skill :

ALWAYS use this skill when :
- A user asks "review this schema", "is this database design good", or pastes DDL for feedback
- Auditing an existing schema, a migration, or a PR that adds or alters tables
- Another agent produced `CREATE TABLE` DDL and it must be checked before it ships

NEVER use this skill for :
- Reviewing SQL queries (use `postgres-agents-query-reviewer`)
- Reviewing a migration's locking and rollout safety (use `postgres-agents-migration-reviewer`)
- Writing new DDL from scratch (use `postgres-core-schema-design` or the relevant `postgres-syntax-*` skill)

## Decision Trees :

### Grading a finding :

```
A check fired. What severity?
  |
  |-- Does it corrupt data integrity or break tenant isolation
  |   (no PK -> no row identity; multi-tenant table with no RLS)?
  |     YES --> CRITICAL. Verdict becomes POOR.
  |
  |-- Does it degrade performance or maintainability at scale
  |   (un-indexed FK, redundant index, normalization defect)?
  |     YES --> WARNING.
  |
  |-- Is the schema correct and workable, just not ideal style?
        YES --> INFO. The author decides.
```

### Is this a defect or a deliberate choice? :

```
A check fired on something that COULD be intentional
(denormalization, a wide jsonb column, a missing NOT NULL)?
  |
  |-- Is there a comment / doc / commit message stating the intent?
  |     YES --> report as INFO "intentional, documented". Do not escalate.
  |
  |-- No stated rationale?
        --> report the finding AND ask the author:
            "Is this deliberate? If so, document why."
            Do NOT silently hard-fail it.
```

### Single table vs the whole schema :

```
About to grade S-1 (PK), S-3 (FK index), S-6 (redundant index), S-10 (normalization)?
  |-- These need the WHOLE schema. A FK points at another table;
  |   a redundant index needs the sibling index; a 3NF violation
  |   needs the dependency graph.
  |   --> Refuse to grade them until every CREATE TABLE is parsed.
  |-- S-4, S-5, S-9, S-12, S-14 are per-column and per-table.
        --> Safe to grade table-by-table.
```

## The Review Checklist :

Run every check. Each check has a trigger, a rule, a severity, and a cite-skill.
Full per-check rationale and DDL examples are in references/methods.md and
references/examples.md.

### S-1 : Table with no primary key

ALWAYS flag a `CREATE TABLE` with no `PRIMARY KEY` and no promotable unique key.
NEVER assume "the app handles identity".
Severity : CRITICAL. Cite : `postgres-core-schema-design`.
WHY : without a PK a row has no stable identity; UPDATE/DELETE by row, logical
replication, and most ORMs all break. Fix : add `id bigint GENERATED ALWAYS AS
IDENTITY PRIMARY KEY` or a real natural key.

### S-2 : Multi-tenant table with no RLS or tenant-scoping strategy

ALWAYS flag a table holding rows for multiple tenants (`tenant_id`, `org_id`,
`account_id` column) that has neither RLS enabled nor a documented scoping plan.
Severity : CRITICAL. Cite : `postgres-core-rls-policies`.
WHY : every query must remember the tenant filter by hand; one forgotten
`WHERE tenant_id = ?` leaks another tenant's data. Fix : `ENABLE ROW LEVEL
SECURITY` + a tenant policy, or document the enforced scoping layer.

### S-3 : Foreign-key column with no index

ALWAYS flag a column with a `REFERENCES` clause that has no covering index.
NEVER assume the FK creates one: it does not.
Severity : WARNING. Cite : `postgres-impl-indexing-strategy`.
WHY : joins on the FK and parent `DELETE`/key-`UPDATE` (cascade enforcement)
seqscan the child table. Fix : `CREATE INDEX ON child (parent_id)`.

### S-4 : SERIAL instead of GENERATED AS IDENTITY

ALWAYS flag `serial` / `bigserial` columns. NEVER treat them as equivalent to
identity columns.
Severity : WARNING. Cite : `postgres-syntax-dml-ddl`.
WHY : `serial` exposes a detached sequence with its own ownership and GRANT
quirks, and `ALTER TYPE` breaks the dependency. Fix : `id bigint GENERATED
ALWAYS AS IDENTITY` (v10+, SQL-standard).

### S-5 : Non-snake_case / camelCase identifiers

ALWAYS flag `camelCase` or `PascalCase` table and column names.
Severity : WARNING. Cite : `postgres-core-schema-design`.
WHY : PostgreSQL folds unquoted identifiers to lower case, so a `camelCase` name
must be double-quoted in EVERY query forever; one missed quote is a silent wrong
identifier. Fix : `snake_case` everywhere.

### S-6 : Redundant index

ALWAYS flag an index whose leading columns are already the prefix of another
index on the same table.
Severity : WARNING. Cite : `postgres-impl-schema-archaeology`.
WHY : an index on `(a)` is fully served by an existing index on `(a, b)`; the
extra index only adds write cost and bloat. Fix : drop the redundant index.

### S-7 : RLS enabled but FORCE missing where the owner runs DML

ALWAYS flag a table with `ENABLE ROW LEVEL SECURITY` whose owning role also runs
application DML, when `FORCE ROW LEVEL SECURITY` is absent.
Severity : WARNING. Cite : `postgres-core-rls-policies`.
WHY : "Table owners normally bypass row security"; if the app connects as the
owner, every policy is silently skipped. Fix : `ALTER TABLE t FORCE ROW LEVEL
SECURITY`, or run the app as a non-owner role.

### S-8 : Structured data stored as text / CSV

ALWAYS flag a `text` column holding delimited or structured data (CSV, key=value
lists, JSON-in-text).
Severity : WARNING. Cite : `postgres-syntax-jsonb`.
WHY : the database cannot index, constrain, or query the inner fields; every
read parses strings in the app. Fix : a `jsonb` column with a GIN index, a real
child table, or a typed array.

### S-9 : timestamp without time zone for event times

ALWAYS flag `timestamp` (alias `timestamp without time zone`) used for a moment
in time (`created_at`, `occurred_at`).
Severity : WARNING. Cite : `postgres-core-schema-design`.
WHY : `timestamp` stores no offset, so the same value means different instants
in different sessions; DST and multi-region break it. Fix : `timestamptz`.

### S-10 : Normalization violation

ALWAYS flag a clear 1NF/2NF/3NF violation: a repeating group (`phone1, phone2,
phone3`), a partial dependency, or a transitive dependency.
Severity : WARNING. Cite : `postgres-core-schema-design`.
WHY : repeating groups and redundant columns cause update anomalies and
inconsistent data. Fix : extract the offending columns into their own table.
Deliberate denormalization is acceptable ONLY when documented with its reason.

### S-11 : Logically required column left nullable

ALWAYS flag a column that is logically mandatory (`email` on a users table, a FK
that every row must have) declared without `NOT NULL`.
Severity : WARNING. Cite : `postgres-core-schema-design`.
WHY : nullable required columns let half-built rows exist; every consumer must
defend against a NULL that should be impossible. Fix : add `NOT NULL`, with a
`DEFAULT` if one is sensible.

### S-12 : money type

ALWAYS flag the `money` type.
Severity : INFO. Cite : `postgres-core-schema-design`.
WHY : `money` has a locale-dependent format and fixed fractional precision tied
to `lc_monetary`. Fix : `numeric(precision, scale)` plus an explicit currency
column.

### S-13 : Application tables in the public schema

ALWAYS flag a production application that puts all its tables in `public`.
Severity : INFO. Cite : `postgres-core-schema-design`.
WHY : `public` is shared and historically broadly privileged; a dedicated schema
gives a clean privilege boundary. Fix : a named schema (`app`, per-tenant, etc.).

### S-14 : char(n) instead of text or varchar

ALWAYS flag `char(n)` (blank-padded `character(n)`).
Severity : INFO. Cite : `postgres-core-schema-design`.
WHY : `char(n)` pads with trailing spaces and gives no performance benefit over
`text` / `varchar` in PostgreSQL. Fix : `text`, or `varchar(n)` when a hard
length cap is intended.

### S-15 : Nullable boolean used as an undocumented tri-state

ALWAYS flag a nullable `boolean` where NULL silently carries a third meaning.
Severity : INFO. Cite : `postgres-core-schema-design`.
WHY : "unknown / not-applicable / pending" hidden in a NULL boolean is invisible
to every reader. Fix : document the NULL meaning, or use an explicit enum / text
status with a `CHECK`.

### S-16 : Missing CHECK constraints for known invariants

ALWAYS flag a column with an obvious domain rule (`price >= 0`, `status IN
(...)`, `end_date >= start_date`) and no `CHECK` enforcing it.
Severity : INFO. Cite : `postgres-core-schema-design`.
WHY : an invariant only in application code is not enforced for other writers,
imports, or manual fixes. Fix : add the `CHECK` constraint.

## Output Format :

Emit findings as a list, then one verdict line. Per finding :

```
[SEVERITY] <schema.table[.column]>
  Problem : <what is structurally wrong>
  Fix     : <concrete DDL change>
  Cite    : <sibling skill>
  Note    : <only if the choice may be deliberate -- ask for rationale>
```

End with exactly one verdict :

```
Verdict : NEEDS-WORK   (0 CRITICAL, 3 WARNING, 2 INFO)
```

`GOOD` requires zero CRITICAL and zero WARNING. Any CRITICAL forces `POOR`. See
references/methods.md for the full report schema.

## Anti-Patterns :

(Reviewer anti-patterns. Full cause + symptom + fix in references/anti-patterns.md)

- Bikeshedding : debating cosmetic naming while a missing PK (S-1) goes unmentioned
- Denormalization-by-ambush : flagging S-10 as a defect without asking if it is deliberate
- Table-in-isolation : grading FK / PK / redundant-index checks without the full FK graph
- Severity inflation : grading a `char(n)` INFO as CRITICAL, or a missing PK as INFO
- Citing nothing : reporting a finding without the sibling skill that fixes it
- Rubber-stamping : declaring a schema GOOD without running all 16 checks

## Reference Links :

- [references/methods.md](references/methods.md) : full 16-check table, severity rubric, verdict rules, report schema
- [references/examples.md](references/examples.md) : complete worked schema reviews, before/after DDL per check, version-annotated
- [references/anti-patterns.md](references/anti-patterns.md) : reviewer anti-patterns with cause + symptom + fix

## See Also :

- postgres-agents-query-reviewer : reviews SQL queries for correctness and performance
- postgres-agents-migration-reviewer : reviews migration safety and locking
- postgres-core-schema-design : naming, types, normalization, schema layout reference
- postgres-core-rls-policies : RLS policy design, FORCE ROW LEVEL SECURITY, tenant isolation
- postgres-impl-indexing-strategy : index access methods, FK indexing, multicolumn indexes
- Vooronderzoek section : §4 (Indexing), §12 (RLS), §19 (Anti-Patterns), §22 (conventions)
- Official docs : https://www.postgresql.org/docs/17/ddl-rowsecurity.html
