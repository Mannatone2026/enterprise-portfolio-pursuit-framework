---
name: postgres-syntax-upsert-merge
description: >
  Use when writing idempotent inserts, multi-action data sync, or migrating from MySQL INSERT ... ON DUPLICATE KEY UPDATE.
  Prevents 23505 unique_violation when ON CONFLICT misconfigured, non-deterministic MERGE source race, and using MERGE when simpler UPSERT suffices.
  Covers INSERT ... ON CONFLICT (v9.5+) conflict_target rules, DO NOTHING vs DO UPDATE, EXCLUDED pseudo-table, MERGE statement (v15+), MERGE-RETURNING (v17+), merge_action(), UPSERT vs MERGE decision tree.
  Keywords: ON CONFLICT, UPSERT, MERGE, EXCLUDED, DO UPDATE, DO NOTHING, merge_action, RETURNING, idempotent insert, INSERT ON DUPLICATE KEY UPDATE, unique violation 23505, how to do upsert in postgres, when to use merge instead of upsert, MERGE cardinality violation, ERROR there is no unique or exclusion constraint matching the ON CONFLICT specification, ERROR MERGE command cannot affect row a second time
license: MIT
compatibility: "Designed for Claude Code. Requires PostgreSQL 15, 16, or 17."
metadata:
  author: OpenAEC-Foundation
  version: "1.0"
---

# postgres-syntax-upsert-merge

## Quick Reference :

PostgreSQL has two idempotent-write primitives :

1. **`INSERT ... ON CONFLICT`** (UPSERT, v9.5+) : single conflict target, single action (DO NOTHING or DO UPDATE). Fast, simple, the default.
2. **`MERGE`** (v15+) : multi-action sync against a source relation. Multiple `WHEN MATCHED` and `WHEN NOT MATCHED` branches, each with INSERT / UPDATE / DELETE / DO NOTHING. v17 adds RETURNING and `WHEN NOT MATCHED BY SOURCE`.

Pick UPSERT for "insert this row, but if it conflicts, update it". Pick MERGE for "take this batch and conditionally INSERT / UPDATE / DELETE depending on per-row state".

Two-line minimum-viable UPSERT :

```sql
INSERT INTO users (id, email, name) VALUES (1, 'a@b.c', 'Alice')
ON CONFLICT (id) DO UPDATE SET email = EXCLUDED.email, name = EXCLUDED.name;
```

Two-line minimum-viable MERGE (v15+) :

```sql
MERGE INTO users t USING new_users s ON t.id = s.id
WHEN MATCHED THEN UPDATE SET email = s.email
WHEN NOT MATCHED THEN INSERT (id, email) VALUES (s.id, s.email);
```

## When To Use This Skill :

ALWAYS use this skill when :

- A user asks "how do I do an upsert in PostgreSQL"
- Translating MySQL `INSERT ... ON DUPLICATE KEY UPDATE` or SQL Server `MERGE`
- Writing data-sync code (ETL, snapshot reconciliation, change-data-capture replay)
- Debugging `ERROR 23505 unique_violation` from an INSERT that "should have been an upsert"
- Debugging `ERROR MERGE command cannot affect row a second time` (cardinality violation)
- Choosing between UPSERT and MERGE for a new data-load pipeline
- Targeting v17 specifically and wanting `RETURNING merge_action()` for audit/event-emit patterns

NEVER use this skill for :

- `UPDATE ... FROM` (a different DML form, not idempotent insert)
- `INSERT ... SELECT` without conflict handling (plain insert, no upsert semantics)
- Bulk-load via `COPY` (see [postgres-impl-bulk-loading](../../postgres-impl/postgres-impl-bulk-loading/SKILL.md))
- Foreign-data-wrapper writes (FDW + MERGE has v17 restrictions, document separately)

## Decision Trees :

### UPSERT or MERGE :

```
Is the operation "insert row, update on conflict" with a single conflict target ?
├── YES → INSERT ... ON CONFLICT (col) DO UPDATE.
└── NO  → Does the operation need multiple actions per row (some INSERT, some UPDATE, some DELETE) ?
         ├── YES → MERGE (v15+).
         └── NO  → Does the source-vs-target reconciliation need DELETE for rows missing from source ?
                  ├── YES → MERGE WITH WHEN NOT MATCHED BY SOURCE THEN DELETE (v17+).
                  └── NO  → INSERT ... ON CONFLICT is simpler.
```

### Which conflict target :

```
Does the table have a single UNIQUE or PRIMARY KEY constraint that defines the conflict ?
├── YES → ON CONFLICT (col1, col2) - column list (planner picks the matching index).
└── NO  → Is there a named constraint you want to target explicitly ?
         ├── YES → ON CONFLICT ON CONSTRAINT constraint_name.
         └── NO  → Is the uniqueness conditional (partial unique index) ?
                  └── YES → ON CONFLICT (col) WHERE predicate — predicate must match the index WHERE clause exactly.
```

### DO NOTHING or DO UPDATE :

```
Should an existing row be modified by the incoming values ?
├── NO  → DO NOTHING (idempotent insert, skip duplicates).
└── YES → DO UPDATE SET col = EXCLUDED.col [, ...] [WHERE condition].
         Use EXCLUDED.* to reference the proposed-but-rejected values.
         Use the target alias / table name to reference the existing row.
```

## Core Syntax :

### INSERT ... ON CONFLICT :

```sql
INSERT INTO target [(col1, col2, ...)] VALUES (...)
ON CONFLICT { (col1, col2, ...) [WHERE pred] | ON CONSTRAINT name }
  DO { NOTHING | UPDATE SET col = expr [, ...] [WHERE condition] }
[RETURNING expr [, ...]];
```

Rules :

- Conflict target MUST match an existing UNIQUE or PRIMARY KEY (NOT DEFERRABLE) index. Column list = arbiter index lookup. Constraint name = explicit binding.
- For partial unique indexes, the `WHERE pred` after the conflict target MUST be a textual subset of the index's WHERE clause. PostgreSQL matches the predicate, not the index name.
- `EXCLUDED` is a pseudo-table containing the row proposed for INSERT. Reference it as `EXCLUDED.col`.
- The existing-row side is referenced by the target table name : `SET name = target_table.name || EXCLUDED.suffix`.
- `RETURNING` returns the actually-inserted-or-updated rows (skips DO NOTHING no-ops).
- ON CONFLICT can NOT use exclusion constraints as arbiters. Only UNIQUE/PK indexes qualify.

### MERGE (v15+) :

```sql
MERGE INTO target_table [ [AS] alias ]
USING { source_table | (subquery) } [ [AS] alias ]
ON join_condition
[ WHEN MATCHED [AND extra_condition] THEN
    { UPDATE SET col = expr [, ...] | DELETE | DO NOTHING } ]
[ WHEN NOT MATCHED [AND extra_condition] THEN
    { INSERT [(cols)] VALUES (exprs) | DO NOTHING } ]
[ WHEN NOT MATCHED BY SOURCE [AND extra_condition] THEN              -- v17+
    { UPDATE SET col = expr [, ...] | DELETE | DO NOTHING } ]
[ RETURNING merge_action(), expr [, ...] ];                          -- v17+
```

Rules :

- WHEN clauses evaluate in order; first match wins per source row. Order them most-specific first.
- The `ON` join condition must produce AT MOST ONE source row per target row. Otherwise : `ERROR MERGE command cannot affect row a second time` (SQLSTATE 21000, cardinality violation).
- `WHEN NOT MATCHED` (without BY SOURCE) is `WHEN NOT MATCHED BY TARGET`. The "BY TARGET" wording was added in v17 but the semantics are unchanged from v15.
- `WHEN NOT MATCHED BY SOURCE` is v17+ : matches target rows with no corresponding source row. Enables full-sync DELETE.
- MERGE cannot target materialized views or foreign tables (v15-v17). MERGE on updatable views with RULES is v17+.
- `merge_action()` returns `'INSERT'` / `'UPDATE'` / `'DELETE'` per RETURNING row (v17+).

### merge_action() Function :

Only valid inside a `MERGE ... RETURNING` clause (v17+). Returns the action that produced the row :

```sql
MERGE INTO users t USING new_users s ON t.id = s.id
WHEN MATCHED THEN UPDATE SET email = s.email
WHEN NOT MATCHED THEN INSERT (id, email) VALUES (s.id, s.email)
RETURNING merge_action() AS action, t.id, t.email;
-- action | id | email
-- UPDATE | 1  | a@b.c
-- INSERT | 2  | c@d.e
```

## Common Errors :

| SQLSTATE | Error message | Cause | Fix |
|---|---|---|---|
| 42P10 | `there is no unique or exclusion constraint matching the ON CONFLICT specification` | Conflict target column has no UNIQUE/PK index, or the partial-index WHERE predicate does not match | Create the index : `CREATE UNIQUE INDEX ON t (col)`. For partial : add matching WHERE clause. |
| 23505 | `duplicate key value violates unique constraint "..."` | INSERT lacks ON CONFLICT, or ON CONFLICT targets the wrong constraint | Add ON CONFLICT (correct_col), or target via ON CONSTRAINT name. |
| 21000 | `MERGE command cannot affect row a second time` | Source produces >1 row per target row (non-deterministic source) | Deduplicate source : `USING (SELECT DISTINCT ON (key) * FROM source ORDER BY key, priority)`. |
| 0A000 | `cannot execute MERGE on relation "..."` | Target is a materialized view, foreign table, or non-updatable view | Use a regular table; for MV, REFRESH separately. |
| 42601 | syntax error near `MERGE` | PostgreSQL version is < 15 | Upgrade to v15+, or use INSERT ... ON CONFLICT. |
| 42601 | syntax error near `RETURNING` (in MERGE) | PostgreSQL version is < 17 | Use a separate SELECT after MERGE, or upgrade to v17+. |

## Anti-Patterns :

The four most-common bugs in upsert/merge code (full list with reproducers in references/anti-patterns.md) :

1. **ON CONFLICT without matching unique index** → `42P10`. The conflict target requires a real UNIQUE/PK constraint, not "intent".
2. **MERGE with non-deterministic source** → `21000` cardinality violation. Deduplicate the source before MERGE.
3. **Using MERGE for simple upsert** when `INSERT ... ON CONFLICT` would do. MERGE is 2-3x more verbose and harder to reason about for the single-conflict-target case.
4. **EXCLUDED reference mistake** : `SET col = col` (no-op against itself) vs `SET col = EXCLUDED.col` (apply proposed value). Easy to typo, silent semantic bug.

## Version Notes :

| Feature | v15 | v16 | v17 |
|---|---|---|---|
| INSERT ... ON CONFLICT | yes (since v9.5) | yes | yes |
| MERGE statement | yes (NEW) | yes | yes |
| MERGE RETURNING | no | no | yes (NEW) |
| merge_action() function | no | no | yes (NEW) |
| WHEN NOT MATCHED BY SOURCE | no | no | yes (NEW) |
| MERGE on updatable view (with RULE) | no | no | yes (NEW) |

If targeting a mixed-version fleet, write `INSERT ... ON CONFLICT` (works everywhere) and only reach for MERGE when the multi-action requirement is real. For RETURNING-from-merge on a v15/v16 fleet, fall back to UPSERT or to a CTE pattern with INSERT/UPDATE separately. See [postgres-core-version-matrix](../../postgres-core/postgres-core-version-matrix/SKILL.md) for the full version table.

## Reference Links :

- [methods.md](references/methods.md) : full `INSERT ... ON CONFLICT` and `MERGE` syntax, conflict-target arbiter rules, EXCLUDED pseudo-table reference, WHEN-clause ordering.
- [examples.md](references/examples.md) : 10 complete patterns including idempotent insert, counter increment, batch sync, full-sync MERGE, RETURNING audit log, partial-unique-index UPSERT.
- [anti-patterns.md](references/anti-patterns.md) : 10 documented bugs with reproducible failure SQL and the official fix.

Official PostgreSQL documentation (verified 2026-05-19) :

- INSERT (ON CONFLICT) : https://www.postgresql.org/docs/17/sql-insert.html
- MERGE : https://www.postgresql.org/docs/17/sql-merge.html
- v17 Release Notes (MERGE RETURNING, WHEN NOT MATCHED BY SOURCE) : https://www.postgresql.org/docs/release/17.0/
- v15 Release Notes (MERGE introduced) : https://www.postgresql.org/docs/release/15.0/
