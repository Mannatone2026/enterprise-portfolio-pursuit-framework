# Anti-Patterns : Constraint Violations

Each entry : the anti-pattern, its root cause, the observable symptom, the
SQLSTATE involved, and the fix pattern. Verified against PostgreSQL 17 docs.

## AP-1 : Catch 23505, then UPDATE as a fallback

**Anti-pattern** : `INSERT`, catch `unique_violation`, then run a separate
`UPDATE` to "upsert".

```python
try:
    cur.execute("INSERT INTO inv (sku, qty) VALUES (%s, %s)", [sku, qty])
except errors.UniqueViolation:
    cur.execute("UPDATE inv SET qty = qty + %s WHERE sku = %s", [qty, sku])  # WRONG
```

**Cause** : the `INSERT` and the `UPDATE` are two statements with a gap between
them. Another transaction can `DELETE` the row in that gap.

**Symptom** : the `UPDATE` silently affects zero rows ; stock or counters drift ;
the bug is intermittent and only appears under concurrency. SQLSTATE `23505` on
the first statement, then a silently lost update.

**Fix** : one atomic statement.

```sql
INSERT INTO inv (sku, qty) VALUES (:sku, :qty)
ON CONFLICT (sku) DO UPDATE SET qty = inv.qty + EXCLUDED.qty;
```

The docs guarantee `ON CONFLICT DO UPDATE` is "an atomic INSERT or UPDATE
outcome ... even under high concurrency".

## AP-2 : No index on a foreign-key child column

**Anti-pattern** : declaring `REFERENCES parent (id)` and assuming the FK
provides an index on the child column.

**Cause** : "the declaration of a foreign key constraint does not automatically
create an index on the referencing columns". Only the parent (referenced) side
is guaranteed to have one.

**Symptom** : `DELETE FROM parent` or an `UPDATE` of the parent key is extremely
slow ; `EXPLAIN` shows a `Seq Scan` on the child table per deleted row ;
cascading deletes lock the child table for minutes. SQLSTATE involved : `23503`
checks become the bottleneck even when they succeed.

**Fix** :

```sql
CREATE INDEX ON child_table (parent_id);
```

Audit with the "FK without index" query in methods.md. Matches vooronderzoek
§19 anti-pattern 2.

## AP-3 : Circular foreign key without DEFERRABLE

**Anti-pattern** : two tables (or one self-referencing table) with mutual FKs,
all `NOT DEFERRABLE` (the default).

**Cause** : a `NOT DEFERRABLE` FK is checked at the end of every statement. The
first row of a mutual pair always references a row that does not exist yet.

**Symptom** : `23503 foreign_key_violation` on the first `INSERT` ; the data set
is genuinely impossible to load in any order.

**Fix** : make the constraint `DEFERRABLE INITIALLY DEFERRED` so both checks run
at `COMMIT`, when both rows exist.

```sql
ALTER TABLE employees
  ADD CONSTRAINT employees_manager_fk
  FOREIGN KEY (manager_id) REFERENCES employees (id)
  DEFERRABLE INITIALLY DEFERRED;
```

For a `DEFERRABLE INITIALLY IMMEDIATE` constraint, defer per transaction with
`SET CONSTRAINTS ALL DEFERRED`.

## AP-4 : App-level existence check instead of a UNIQUE constraint

**Anti-pattern** : `SELECT 1 FROM users WHERE email = $1` ; if no row, `INSERT`.
No `UNIQUE` constraint on `email`.

**Cause** : the check and the insert are separate statements. Two concurrent
requests both see "no row" and both insert.

**Symptom** : duplicate rows that "should be impossible" ; no error is ever
raised because there is no constraint to raise one ; data corruption discovered
much later.

**Fix** : let the database enforce it and handle the error.

```sql
ALTER TABLE users ADD CONSTRAINT users_email_key UNIQUE (email);
```

Then `INSERT ... ON CONFLICT (email) DO NOTHING`, or catch `23505`. The
constraint is enforced atomically inside the write ; no race window exists.

## AP-5 : Swallowing a class 23 error silently

**Anti-pattern** : `try: insert() except: pass`, or catching every constraint
error as "row already exists".

**Cause** : a bare catch treats every class 23 SQLSTATE as the same benign
event. `23505` from a composite unique index, `23514` from a bad value, and
`23503` from a missing parent all get hidden.

**Symptom** : writes silently disappear ; invalid data is accepted upstream ;
the real conflict surfaces as inconsistent state somewhere unrelated.

**Fix** : branch on `sqlstate` and `constraint_name`. Handle the one violation
you expect ; re-raise everything else.

```python
except errors.UniqueViolation as e:
    if e.diag.constraint_name != 'users_email_key':
        raise                       # a different unique index fired
    handle_duplicate_email()
```

## AP-6 : Weakening a CHECK to make 23514 stop

**Anti-pattern** : a `CHECK (total >= 0)` raises `23514`, so the constraint is
dropped or relaxed to `CHECK (total >= -1000000)`.

**Cause** : misreading the error. `23514` means the application produced a value
the business rules forbid. The constraint is correct ; the input is wrong.

**Symptom** : after relaxing the CHECK, invalid values (negative totals, bad
states) enter the table and break reports and downstream logic.

**Fix** : keep the CHECK. Validate and reject the bad value at the application
boundary, and fix the code path that produced it. Treat `23514` as a caught bug,
not as noise. Remember a CHECK passes on NULL operands ; add `NOT NULL`
separately if NULL must be forbidden.

## AP-7 : Plain ADD CONSTRAINT on a large, busy table

**Anti-pattern** : `ALTER TABLE big ADD CONSTRAINT ... CHECK (...)` (or a foreign
key) during traffic, without `NOT VALID`.

**Cause** : a normal `ADD CONSTRAINT` scans every existing row to verify the
constraint, holding a strong lock for the whole scan.

**Symptom** : writes and reads on the table stall for the duration of the scan ;
on a multi-million-row table this is minutes of blocked traffic.

**Fix** : two steps. Add with `NOT VALID` (skips the scan, brief lock), then
`VALIDATE CONSTRAINT` off-peak under `SHARE UPDATE EXCLUSIVE`, which "does not
need to lock out concurrent updates".

```sql
ALTER TABLE big ADD CONSTRAINT big_ck CHECK (col >= 0) NOT VALID;
ALTER TABLE big VALIDATE CONSTRAINT big_ck;
```

`NOT VALID` is supported for foreign key and CHECK constraints only.

## AP-8 : Treating 23502 as a column problem

**Anti-pattern** : a `not_null_violation` on a column, so the column is changed
to `DROP NOT NULL` to make the error stop.

**Cause** : `23502` means the application sent a NULL (or omitted a value with
no DEFAULT) for a column that is genuinely required. The column constraint is
not the bug.

**Symptom** : after dropping `NOT NULL`, rows with NULL in a required column
enter the table ; queries and joins that assume the value break later.

**Fix** : keep `NOT NULL`. Either fix the writer to always supply the value, or
add a column `DEFAULT` if a sensible default exists.

```sql
ALTER TABLE events ALTER COLUMN created_at SET DEFAULT now();
```

## AP-9 : Using ON CONFLICT with no matching unique index

**Anti-pattern** : `INSERT ... ON CONFLICT (some_col) DO UPDATE ...` where
`some_col` has no unique index or constraint.

**Cause** : `ON CONFLICT` inference requires an arbiter : a unique index or
constraint exactly covering the conflict-target columns. Without one there is
nothing to infer.

**Symptom** : `ERROR: there is no unique or exclusion constraint matching the
ON CONFLICT specification` (SQLSTATE `42P10`). The upsert never runs.

**Fix** : create the unique constraint the upsert depends on first.

```sql
ALTER TABLE t ADD CONSTRAINT t_key UNIQUE (some_col);
```

Exclusion constraints cannot be `ON CONFLICT DO UPDATE` arbiters at all ; only
`NOT DEFERRABLE` unique indexes and constraints qualify.
