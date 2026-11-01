# Methods : Schema Reviewer

The full review apparatus for `postgres-agents-schema-reviewer`: the 16-check
table, the severity rubric, the verdict rules, and the report schema. Verified
against PostgreSQL 17 official documentation and the package vooronderzoek.

## The 16-check table

| ID | Trigger | Severity | Cite |
|----|---------|----------|------|
| S-1 | table with no `PRIMARY KEY` and no promotable unique key | CRITICAL | postgres-core-schema-design |
| S-2 | multi-tenant table, no RLS and no documented scoping strategy | CRITICAL | postgres-core-rls-policies |
| S-3 | `REFERENCES` column with no covering index | WARNING | postgres-impl-indexing-strategy |
| S-4 | `serial` / `bigserial` column | WARNING | postgres-syntax-dml-ddl |
| S-5 | `camelCase` / `PascalCase` identifier | WARNING | postgres-core-schema-design |
| S-6 | index whose leading columns prefix another index | WARNING | postgres-impl-schema-archaeology |
| S-7 | RLS enabled, owner runs DML, no `FORCE ROW LEVEL SECURITY` | WARNING | postgres-core-rls-policies |
| S-8 | structured data stored in a `text` column (CSV, kv, JSON-in-text) | WARNING | postgres-syntax-jsonb |
| S-9 | `timestamp` (without time zone) for an event time | WARNING | postgres-core-schema-design |
| S-10 | 1NF / 2NF / 3NF violation | WARNING | postgres-core-schema-design |
| S-11 | logically required column left nullable | WARNING | postgres-core-schema-design |
| S-12 | `money` type | INFO | postgres-core-schema-design |
| S-13 | application tables in the `public` schema | INFO | postgres-core-schema-design |
| S-14 | `char(n)` / `character(n)` | INFO | postgres-core-schema-design |
| S-15 | nullable `boolean` used as an undocumented tri-state | INFO | postgres-core-schema-design |
| S-16 | known invariant with no `CHECK` constraint | INFO | postgres-core-schema-design |

## Severity rubric

| Severity | Definition | Effect on verdict |
|----------|------------|-------------------|
| CRITICAL | corrupts data integrity or breaks tenant isolation | verdict is `POOR` |
| WARNING | degrades performance or maintainability at scale | verdict is at best `NEEDS-WORK` |
| INFO | style or context-dependent, the author decides | no effect on verdict |

Grade by CONSEQUENCE, not by how easy the fix looks. A missing PK (S-1) is a
one-line fix but CRITICAL because it corrupts row identity. A redundant index
(S-6) is also a one-line fix but only WARNING.

## Verdict rules

```
count CRITICAL, WARNING, INFO across all findings :
  any CRITICAL > 0                     -> POOR
  CRITICAL == 0 and WARNING > 0         -> NEEDS-WORK
  CRITICAL == 0 and WARNING == 0        -> GOOD   (INFO findings allowed)
```

Exactly one verdict per review. INFO findings never block `GOOD`.

## The deliberate-choice rule

Several checks fire on things that may be intentional: S-10 (denormalization),
S-8 (a wide jsonb column chosen on purpose), S-11 (a column nullable by design),
S-15 (a documented tri-state), S-13 (`public` acceptable for a small internal
tool).

For these :
1. If the DDL, a comment, or a commit message states the rationale, report the
   finding as INFO "intentional, documented" and do not escalate.
2. If no rationale is stated, still report the finding, and add a `Note:` line
   asking the author to confirm intent and document it.
3. NEVER silently hard-fail a choice that has a plausible deliberate reason.

## FK-graph-first rule

S-1, S-3, S-6, and S-10 cannot be graded from a single `CREATE TABLE`:
- S-1 : a child table may rely on a composite key that includes a FK.
- S-3 : the `REFERENCES` target is another table.
- S-6 : "redundant" is defined relative to the other indexes on the table.
- S-10 : a transitive dependency spans columns and sometimes tables.

ALWAYS parse every `CREATE TABLE` and build the foreign-key graph BEFORE
grading. Reviewing one table in isolation produces false negatives on exactly
the highest-severity checks.

## Detection notes per check

- **S-1** : a PK can be a single column, a composite key, or a promotable
  `UNIQUE NOT NULL` set. A bare `UNIQUE` on a nullable column is NOT a PK.
- **S-2** : the tenant signal is a column like `tenant_id`, `org_id`,
  `account_id`, `company_id`, or `workspace_id`. Confirm with the author if the
  table is genuinely single-tenant.
- **S-3** : the parent (referenced) side always has an index; only the child
  (referencing) side needs the check. A multicolumn index whose leading column
  is the FK column satisfies S-3.
- **S-4** : `serial`/`bigserial`/`smallserial` are not real types; they expand
  to an integer column plus an owned sequence plus a `DEFAULT`.
- **S-5** : an identifier is suspect when it contains an uppercase letter and is
  therefore double-quoted in the DDL.
- **S-6** : index `(a)` is redundant against `(a, b)`. Index `(b)` is NOT
  redundant against `(a, b)` because a multicolumn B-tree only serves leading
  prefixes.
- **S-7** : this fires only when the connecting role is also the table owner.
  If the app already runs as a non-owner role, S-7 does not apply.
- **S-9** : `date` for a calendar date and `time` for a wall-clock time are
  fine; S-9 targets only a moment-in-time stored as `timestamp`.

## RLS facts behind S-2 and S-7

Source : https://www.postgresql.org/docs/17/ddl-rowsecurity.html

- "When row security is enabled on a table ... all normal access to the table
  ... must be allowed by a row security policy ... If no policy exists for the
  table, a default-deny policy is used".
- "Superusers and roles with the `BYPASSRLS` attribute always bypass the row
  security system when accessing a table. Table owners normally bypass row
  security as well, though a table owner can choose to be subject to row
  security with `ALTER TABLE ... FORCE ROW LEVEL SECURITY`."

Consequence for the reviewer : enabling RLS is not enough if the application
connects as the table owner. S-7 exists precisely because `ENABLE` without
`FORCE` is a silent no-op for the owner.

## Report schema

```
PostgreSQL Schema Review
========================
Scope   : <N tables, M findings>

[CRITICAL] <schema.table[.column]>
  Problem : <structural defect>
  Fix     : <concrete DDL>
  Cite    : <sibling skill>

[WARNING] <schema.table[.column]>
  Problem : ...
  Fix     : ...
  Cite    : ...
  Note    : <ask for rationale if the choice may be deliberate>

[INFO] <schema.table[.column]>
  Problem : ...
  Fix     : ...
  Cite    : ...

Verdict : <GOOD | NEEDS-WORK | POOR>   (x CRITICAL, y WARNING, z INFO)
```

Order findings CRITICAL, then WARNING, then INFO. The verdict line is mandatory
and appears exactly once.
