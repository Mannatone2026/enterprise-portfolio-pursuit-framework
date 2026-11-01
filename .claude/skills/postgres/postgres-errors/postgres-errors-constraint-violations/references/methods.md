# Methods : Constraint Violations

API surface for diagnosing and preventing class 23 integrity constraint
violations. All content verified against PostgreSQL 17 official documentation.

## Class 23 SQLSTATE codes (integrity_constraint_violation)

Source : https://www.postgresql.org/docs/17/errcodes-appendix.html

| SQLSTATE | Condition name | Raised when |
|----------|----------------|-------------|
| `23000` | `integrity_constraint_violation` | generic class code, rarely surfaced alone |
| `23001` | `restrict_violation` | a `RESTRICT` referential action blocks the write |
| `23502` | `not_null_violation` | a NULL is written to a `NOT NULL` column |
| `23503` | `foreign_key_violation` | a referenced row is missing, or child rows block a parent delete/update |
| `23505` | `unique_violation` | a duplicate key against a `UNIQUE` or `PRIMARY KEY` |
| `23514` | `check_violation` | a `CHECK` expression evaluates to false |
| `23P01` | `exclusion_violation` | an `EXCLUDE` constraint conflict |

Notes :
- A blocked parent `DELETE`/`UPDATE` normally surfaces as `23503` with the
  message "update or delete on table ... violates foreign key constraint". The
  distinct `23001 restrict_violation` code exists for the class but is rarely
  the code an application sees ; treat `23503` as the primary FK error code.
- A class 23 error is NEVER retryable as-is, unlike `40001`/`40P01`. The fix is
  the data or the schema, not a retry.

## Error diagnostics fields

Every constraint error exposes structured diagnostics. Field names below are
the libpq `PG_DIAG_*` fields, exposed by drivers (psycopg `e.diag.*`, node-pg
`err.*`, JDBC `SQLException` + `PSQLException.getServerErrorMessage()`).

| Diagnostic field | Contains | Use |
|------------------|----------|-----|
| `sqlstate` | the 5-char code, e.g. `23505` | branch logic by error class |
| `constraint_name` | the violated constraint, e.g. `users_email_key` | identify which rule fired |
| `table_name` | the table owning the constraint | locate the write |
| `column_name` | the column (set for `23502`) | pinpoint the NULL column |
| `message_primary` | the human message | logging |
| `message_detail` | `DETAIL:` line, e.g. `Key (email)=(...) already exists.` | the conflicting key value |

ALWAYS branch on `sqlstate` and `constraint_name`, NEVER on message text :
message wording is not a stable API.

## Constraint clauses (CREATE TABLE / ALTER TABLE)

Source : https://www.postgresql.org/docs/17/ddl-constraints.html

| Constraint | Syntax | Auto-created index | Deferrable |
|------------|--------|--------------------|------------|
| `NOT NULL` | `col type NOT NULL` | none | no, always immediate |
| `CHECK` | `CHECK (expr)` or `CONSTRAINT name CHECK (expr)` | none | no, always immediate |
| `UNIQUE` | `UNIQUE (cols)` | unique B-tree | yes |
| `PRIMARY KEY` | `PRIMARY KEY (cols)` | unique B-tree, forces `NOT NULL` | yes |
| `FOREIGN KEY` | `col type REFERENCES parent (col)` | none (referencing side) | yes |
| `EXCLUDE` | `EXCLUDE USING gist (col WITH op)` | index of the named type | yes |

Key behaviours :
- A `CHECK` constraint "is satisfied if the check expression evaluates to true
  or the null value". NULL operands make most CHECKs pass ; use a separate
  `NOT NULL` to forbid NULL.
- `UNIQUE` : "By default, two null values are not considered equal", so a
  unique column can hold many NULL rows. Add `NULLS NOT DISTINCT` (v15+) to
  treat NULLs as equal.
- A `FOREIGN KEY` must reference a primary key, a unique constraint, or a
  non-partial unique index on the parent. The parent side always has an index ;
  the referencing (child) side does NOT get one automatically.

## Foreign key referential actions

`ON DELETE action` and `ON UPDATE action`. Source : ddl-constraints.html.

| Action | On parent DELETE | On parent key UPDATE |
|--------|------------------|----------------------|
| `NO ACTION` (default) | error if children exist ; check can be deferred | same |
| `RESTRICT` | error if children exist ; check CANNOT be deferred | same |
| `CASCADE` | child rows are deleted too | new key copied into children |
| `SET NULL` | child FK column(s) set to NULL | child FK column(s) set to NULL |
| `SET DEFAULT` | child FK column(s) set to their DEFAULT | same |

- `NO ACTION` vs `RESTRICT` : "the essential difference ... is that `NO ACTION`
  allows the check to be deferred until later in the transaction, whereas
  `RESTRICT` does not".
- `SET DEFAULT` does not excuse constraints : "if the default value would not
  satisfy the foreign key constraint, the operation will fail".
- `SET NULL`/`SET DEFAULT` may name a column subset : `ON DELETE SET NULL (col)`.
- `MATCH FULL` : a child row escapes the FK only if ALL referencing columns are
  NULL ; a mix of NULL and non-NULL always fails under `MATCH FULL`.

## ON CONFLICT clause (INSERT)

Source : https://www.postgresql.org/docs/17/sql-insert.html

```sql
INSERT INTO table ... [ ON CONFLICT [ conflict_target ] conflict_action ]

conflict_target :
    ( { index_column | ( index_expression ) } [ COLLATE coll ] [ opclass ] [, ...] )
        [ WHERE index_predicate ]      -- for inferring a partial unique index
  | ON CONSTRAINT constraint_name

conflict_action :
    DO NOTHING
  | DO UPDATE SET column = { expression | DEFAULT } [, ...] [ WHERE condition ]
```

| Element | Meaning |
|---------|---------|
| `conflict_target` columns | infers the *arbiter* : every unique index containing exactly those columns |
| `ON CONSTRAINT name` | names the arbiter explicitly instead of inferring it |
| `DO NOTHING` | the conflicting row is skipped, no error |
| `DO UPDATE SET ...` | the existing conflicting row is updated (upsert) |
| `EXCLUDED` | special table holding the row proposed for insertion |
| `WHERE condition` on `DO UPDATE` | only rows matching it are updated ; evaluated after the conflict is found |

Rules :
- Only `NOT DEFERRABLE` unique indexes and constraints can be arbiters.
  "exclusion constraints are not supported as arbiters with `ON CONFLICT DO
  UPDATE`".
- `ON CONFLICT DO UPDATE` "guarantees an atomic INSERT or UPDATE outcome ...
  even under high concurrency".
- A statement may not affect one existing row twice : duplicate rows in a
  multi-row `VALUES` raise a cardinality violation.

## Deferral commands

Source : https://www.postgresql.org/docs/17/sql-set-constraints.html

```sql
-- at constraint creation :
... REFERENCES parent (col) DEFERRABLE INITIALLY DEFERRED
... REFERENCES parent (col) DEFERRABLE INITIALLY IMMEDIATE
... REFERENCES parent (col) NOT DEFERRABLE          -- default

-- per transaction :
SET CONSTRAINTS { ALL | name [, ...] } { DEFERRED | IMMEDIATE };
```

| Designation | Initial mode | Changeable by `SET CONSTRAINTS` |
|-------------|--------------|---------------------------------|
| `DEFERRABLE INITIALLY DEFERRED` | deferred (checked at COMMIT) | yes |
| `DEFERRABLE INITIALLY IMMEDIATE` | immediate (checked per statement) | yes |
| `NOT DEFERRABLE` | immediate | no |

- IMMEDIATE constraints are "checked at the end of each statement" ; DEFERRED
  constraints are "not checked until transaction commit".
- Only `UNIQUE`, `PRIMARY KEY`, `REFERENCES`, and `EXCLUDE` are affected.
  "`NOT NULL` and `CHECK` constraints are always checked immediately".
- `SET CONSTRAINTS` only affects the current transaction ; outside a
  transaction block it emits a warning and does nothing.

## NOT VALID and VALIDATE CONSTRAINT

Source : https://www.postgresql.org/docs/17/sql-altertable.html

```sql
ALTER TABLE t ADD CONSTRAINT name FOREIGN KEY (col) REFERENCES p (col) NOT VALID;
ALTER TABLE t ADD CONSTRAINT name CHECK (expr) NOT VALID;
ALTER TABLE t VALIDATE CONSTRAINT name;
```

| Command | Effect | Lock |
|---------|--------|------|
| `ADD CONSTRAINT ... NOT VALID` | adds the constraint, SKIPS the scan of existing rows ; enforced on all new/modified rows | brief strong lock, no full scan |
| `VALIDATE CONSTRAINT` | scans existing rows to confirm they satisfy the constraint, marks it valid | `SHARE UPDATE EXCLUSIVE`, does not block concurrent writes |

- `NOT VALID` is "currently only allowed for foreign key and CHECK constraints".
- `VALIDATE CONSTRAINT` "does not need to lock out concurrent updates, since it
  knows that other transactions will be enforcing the constraint for rows that
  they insert or update ; only pre-existing rows need to be checked".

## Diagnostic queries

```sql
-- all constraints on a table, with type and definition
SELECT conname, contype, condeferrable, condeferred,
       pg_get_constraintdef(oid) AS definition
FROM pg_constraint
WHERE conrelid = 'myschema.mytable'::regclass
ORDER BY contype, conname;
-- contype : c=check, f=foreign key, p=primary key, u=unique, x=exclusion, n=not null

-- foreign keys whose child column has NO supporting index (slow cascades)
SELECT c.conrelid::regclass AS child_table,
       a.attname            AS fk_column,
       c.conname            AS fk_constraint
FROM pg_constraint c
JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey)
WHERE c.contype = 'f'
  AND NOT EXISTS (
    SELECT 1 FROM pg_index i
    WHERE i.indrelid = c.conrelid
      AND (c.conkey::int[])[1] = i.indkey[0]
  );
```

`contype` values : `c` check, `f` foreign key, `p` primary key, `u` unique,
`x` exclusion, `n` not null.
