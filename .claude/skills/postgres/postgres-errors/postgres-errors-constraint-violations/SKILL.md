---
name: postgres-errors-constraint-violations
description: >
  Use when inserts or updates fail with unique, foreign-key, not-null, check, or exclusion constraint errors.
  Prevents the catch-23505-then-UPDATE race (use ON CONFLICT atomically), slow cascading deletes from un-indexed FK columns, and impossible inserts from circular FKs without DEFERRABLE.
  Covers SQLSTATE 23505 unique_violation, 23503 foreign_key_violation, 23502 not_null_violation, 23514 check_violation, 23P01 exclusion_violation, deferrable constraints, FK indexing, NOT VALID constraints, ON CONFLICT handling.
  Keywords: constraint violation, 23505, unique violation, 23503, foreign key violation, 23502, not null violation, 23514, check violation, exclusion violation, deferrable constraint, duplicate key error, foreign key error, insert fails, cannot delete row referenced, ON CONFLICT
license: MIT
compatibility: "Designed for Claude Code. Requires PostgreSQL 15, 16, or 17."
metadata:
  author: OpenAEC-Foundation
  version: "1.0"
---

# postgres-errors-constraint-violations

## Quick Reference :

A constraint violation is PostgreSQL refusing a write that would break data
integrity. Every one of them carries a SQLSTATE in **class 23
(integrity_constraint_violation)**. The class is never a database bug : it is
the database doing its job. The fix is always in the schema or the application,
never in suppressing the error.

```
ERROR:  duplicate key value violates unique constraint "users_email_key"
DETAIL: Key (email)=(a@b.com) already exists.          -- SQLSTATE 23505
```

The six class 23 codes you will meet :

| SQLSTATE | Condition | Meaning |
|----------|-----------|---------|
| `23505` | `unique_violation` | a duplicate key against a UNIQUE / PRIMARY KEY |
| `23503` | `foreign_key_violation` | referenced row missing, OR a delete is blocked by children |
| `23502` | `not_null_violation` | a NULL went into a NOT NULL column |
| `23514` | `check_violation` | a CHECK expression evaluated to false |
| `23P01` | `exclusion_violation` | an EXCLUDE constraint conflict (overlapping range, etc.) |
| `23001` | `restrict_violation` | a RESTRICT action blocked the write |

ALWAYS read the error `DETAIL` line first : it names the exact constraint and
the conflicting key value. NEVER catch a class 23 error and retry blindly : a
`23505` retried as `INSERT` then `UPDATE` is a race, not a fix. Use
`INSERT ... ON CONFLICT DO UPDATE`, which the docs guarantee is atomic.

## When To Use This Skill :

ALWAYS use this skill when :
- A write fails with a class 23 SQLSTATE (`23505`, `23503`, `23502`, `23514`, `23P01`, `23001`)
- You see "duplicate key value", "violates foreign key constraint", or "violates check constraint"
- A `DELETE` on a parent table is slow or blocked by child rows
- You need an idempotent or upsert insert and are tempted to catch the error
- Inserting two rows that reference each other and the FK rejects both
- Adding a constraint to a large, busy table

NEVER use this skill for :
- `40P01 deadlock_detected` or `40001 serialization_failure` (use postgres-errors-serialization / postgres-errors-deadlocks)
- Designing the UPSERT/MERGE syntax itself (use postgres-syntax-upsert-merge)
- `ACCESS EXCLUSIVE` lock waits during DDL (use postgres-impl-zero-downtime-migrations)

## Decision Trees :

### Which class 23 error, and what to do :

```
Write failed. Read the SQLSTATE and the DETAIL line :
├── 23505 unique_violation
│      -> a row with that key already exists.
│         Intentional collision? -> INSERT ... ON CONFLICT DO UPDATE / DO NOTHING.
│         Unexpected? -> the key is wider/narrower than you think; read DETAIL.
├── 23503 foreign_key_violation
│      -> "violates foreign key constraint" on INSERT/UPDATE
│            -> the referenced parent row does not exist. Insert the parent first.
│      -> "update or delete on table ... violates" on DELETE/UPDATE of parent
│            -> child rows still reference it. Choose an ON DELETE action.
├── 23502 not_null_violation
│      -> a NULL reached a NOT NULL column. Almost always an app bug
│         or a missing column DEFAULT. Fix the writer, not the column.
├── 23514 check_violation
│      -> a CHECK expression returned false. The value is genuinely invalid;
│         reject it upstream. NEVER weaken the CHECK to make the error stop.
├── 23P01 exclusion_violation
│      -> two rows conflict under an EXCLUDE constraint (e.g. overlapping
│         time ranges). Adjust the new row or detect the overlap first.
└── 23001 restrict_violation
       -> a RESTRICT foreign-key action blocked the write. Remove the
          referencing rows first, or switch the action to CASCADE/SET NULL.
```

### Idempotent insert : which ON CONFLICT action :

```
You want INSERT to not fail when the row already exists.
├── "Insert if absent, otherwise leave it alone"
│      -> ON CONFLICT (cols) DO NOTHING
├── "Insert if absent, otherwise refresh some columns" (upsert)
│      -> ON CONFLICT (cols) DO UPDATE SET c = EXCLUDED.c
├── Conflict only on a named constraint
│      -> ON CONFLICT ON CONSTRAINT constraint_name DO ...
└── Conflict target is an EXCLUDE constraint
       -> NOT supported as an arbiter. Use MERGE or catch 23P01 explicitly.
```

### ON DELETE action for a foreign key :

```
What should happen to child rows when the parent is deleted?
├── Children are meaningless without the parent (order_items <- orders)
│      -> ON DELETE CASCADE
├── Children outlive the parent, the link just clears (posts.author_id)
│      -> ON DELETE SET NULL   (the FK column must be nullable)
├── Children fall back to a sentinel parent
│      -> ON DELETE SET DEFAULT (the default must satisfy the FK)
├── Deleting a referenced parent is always a mistake, fail loud, now
│      -> ON DELETE RESTRICT   (cannot be deferred)
└── Same as RESTRICT but allow deferring the check to COMMIT
       -> ON DELETE NO ACTION  (the default)
```

## Patterns :

### Pattern 1 : Atomic upsert instead of catch-23505-then-UPDATE

ALWAYS resolve an expected duplicate with `INSERT ... ON CONFLICT DO UPDATE` in
one statement. NEVER `INSERT`, catch `23505`, then `UPDATE` as a fallback.

```sql
INSERT INTO inventory (sku, qty)
VALUES ('A-1', 10)
ON CONFLICT (sku)
DO UPDATE SET qty = inventory.qty + EXCLUDED.qty;
```

WHY : the catch-then-UPDATE path has a race window. Between your failed `INSERT`
and your `UPDATE`, another transaction can delete the row, and the `UPDATE`
silently affects zero rows. The docs state `ON CONFLICT DO UPDATE` "guarantees
an atomic INSERT or UPDATE outcome ... even under high concurrency". `EXCLUDED`
is the special table holding the row you proposed to insert.

### Pattern 2 : ON CONFLICT DO NOTHING for idempotent inserts

ALWAYS use `ON CONFLICT ... DO NOTHING` when re-running an insert must be a
no-op. NEVER rely on "the error just means it already exists" and swallow it.

```sql
INSERT INTO tags (name) VALUES ('postgres')
ON CONFLICT (name) DO NOTHING;
```

WHY : a swallowed `23505` hides every OTHER cause of a `23505` (a different
unique index firing, a wider composite key). `DO NOTHING` makes "skip on
conflict" explicit and scoped to one arbiter index, so unrelated violations
still surface.

### Pattern 3 : Index every foreign-key referencing column

ALWAYS create an index on the child (referencing) column of a foreign key.
NEVER assume the FK creates one : it does not.

```sql
CREATE TABLE order_items (
    id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_id  bigint NOT NULL REFERENCES orders (id) ON DELETE CASCADE,
    sku       text   NOT NULL
);
CREATE INDEX ON order_items (order_id);   -- the FK does NOT create this
```

WHY : the docs are explicit. The *referenced* side always has an index, but
"the declaration of a foreign key constraint does not automatically create an
index on the referencing columns". Without it, every parent `DELETE` or key
`UPDATE` does a sequential scan of the child table to enforce the FK. On a large
child table that turns a cascading delete into minutes of locked scanning.

### Pattern 4 : DEFERRABLE INITIALLY DEFERRED for circular references

ALWAYS make a foreign key `DEFERRABLE INITIALLY DEFERRED` when two tables
reference each other or you must insert rows out of dependency order. NEVER try
to insert a cycle under the default `NOT DEFERRABLE` constraint : it is
impossible.

```sql
ALTER TABLE employees
  ADD CONSTRAINT employees_manager_fk
  FOREIGN KEY (manager_id) REFERENCES employees (id)
  DEFERRABLE INITIALLY DEFERRED;

BEGIN;
  INSERT INTO employees (id, manager_id) VALUES (1, 2);  -- 2 not there yet, OK
  INSERT INTO employees (id, manager_id) VALUES (2, 1);
COMMIT;                                                  -- both checked here
```

WHY : a `NOT DEFERRABLE` FK is checked at the end of every statement, so the
first insert of a mutual pair always fails. The docs : a deferred constraint is
"not checked until transaction commit". Only `UNIQUE`, `PRIMARY KEY`,
`REFERENCES`, and `EXCLUDE` can be deferred. `NOT NULL` and `CHECK` are "always
checked immediately". Use `SET CONSTRAINTS ALL DEFERRED` to defer per
transaction when the constraint is `DEFERRABLE INITIALLY IMMEDIATE`.

### Pattern 5 : NOT VALID then VALIDATE to add a constraint without a long lock

ALWAYS add a CHECK or FOREIGN KEY constraint to a large, busy table with
`NOT VALID`, then `VALIDATE CONSTRAINT` in a separate step. NEVER add a plain
constraint to a big table during traffic : the full verification scan blocks.

```sql
ALTER TABLE orders
  ADD CONSTRAINT orders_total_positive CHECK (total >= 0) NOT VALID;

-- later, off-peak : scans existing rows under SHARE UPDATE EXCLUSIVE
ALTER TABLE orders VALIDATE CONSTRAINT orders_total_positive;
```

WHY : a normal `ADD CONSTRAINT` scans every existing row to verify it, holding a
strong lock. `NOT VALID` "skips this potentially-lengthy scan" and still
enforces the constraint on all subsequent inserts and updates. `VALIDATE
CONSTRAINT` then checks the old rows under a `SHARE UPDATE EXCLUSIVE` lock that
"does not need to lock out concurrent updates". `NOT VALID` is allowed only for
foreign key and CHECK constraints.

### Pattern 6 : Read the error DETAIL to find the exact constraint

ALWAYS surface the SQLSTATE, constraint name, and `DETAIL` line from the error
object. NEVER log only the generic message text.

```python
# psycopg 3 : the diagnostics name the constraint and the key
import psycopg
from psycopg import errors

try:
    cur.execute("INSERT INTO users (email) VALUES (%s)", [email])
except errors.UniqueViolation as e:
    d = e.diag
    # d.sqlstate -> '23505', d.constraint_name -> 'users_email_key'
    # d.message_detail -> 'Key (email)=(a@b.com) already exists.'
    raise ValueError(f"email already registered: {d.constraint_name}")
```

WHY : the `DETAIL` line names the constraint that fired and the conflicting key
values. A composite unique index or a partial index can fire on a column you did
not expect ; the constraint name removes the guesswork. Mapping `sqlstate` to a
typed application error also lets you distinguish a `23505` (retryable as upsert)
from a `23514` (a genuine bad value, never retryable).

### Pattern 7 : Enforce invariants in the database, not only in the app

ALWAYS put uniqueness, referential integrity, and value rules in DB constraints.
NEVER rely on an application-level "check if it exists, then insert".

```sql
-- the constraint is the single source of truth, race-proof by construction
ALTER TABLE accounts ADD CONSTRAINT accounts_email_key UNIQUE (email);
ALTER TABLE accounts ADD CONSTRAINT accounts_balance_ck CHECK (balance >= 0);
```

WHY : an app-level `SELECT ... ; if not found INSERT` has a race window between
the check and the insert ; two concurrent requests both pass the check and both
insert. The database constraint is enforced atomically inside the write, so a
class 23 error is the *correct* outcome of a real conflict. Keep the constraint
and handle the error ; do not replace the constraint with app logic.

## Anti-Patterns :

(Full cause + symptom + SQLSTATE + fix in references/anti-patterns.md)

- Catch `23505` then `UPDATE` as a fallback : race window, use `ON CONFLICT DO UPDATE`
- No index on a foreign-key child column : sequential-scan cascading deletes (`23503` slow)
- Circular FK without `DEFERRABLE` : the pair can never be inserted
- App-level existence check instead of a UNIQUE constraint : check-then-insert race
- Swallowing a class 23 error silently : hides the real conflict and corrupts state
- Weakening a CHECK to stop a `23514` : the data is invalid, the constraint was right
- Plain `ADD CONSTRAINT` on a large live table : the verify scan blocks writes
- Treating `23502` as a column problem : it is a missing DEFAULT or an app bug

## Reference Links :

- [references/methods.md](references/methods.md) : Class 23 SQLSTATE table, constraint clauses, ON CONFLICT grammar, deferral and validation commands
- [references/examples.md](references/examples.md) : Working upsert, deferred-constraint, NOT VALID, and error-handling code, version-annotated
- [references/anti-patterns.md](references/anti-patterns.md) : Anti-patterns with cause, symptom, SQLSTATE, and fix

## See Also :

- postgres-syntax-upsert-merge : full `INSERT ON CONFLICT` and `MERGE` syntax
- postgres-errors-serialization : `40001` retry logic, contrasted with non-retryable `23xxx`
- postgres-errors-deadlocks : `40P01` and lock-ordering, separate from integrity errors
- postgres-impl-zero-downtime-migrations : adding constraints online with `NOT VALID`
- Vooronderzoek section : §19 (Anti-Patterns), items 2 and 9
- Official docs : https://www.postgresql.org/docs/17/ddl-constraints.html
- Official docs : https://www.postgresql.org/docs/17/errcodes-appendix.html
