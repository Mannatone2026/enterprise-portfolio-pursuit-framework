# Examples : Constraint Violations

Working, verified examples for handling and preventing class 23 integrity
constraint violations. Version annotations : `v15+` works on 15, 16, 17 ;
`v15-only` notes a feature added in that version.

## Example 1 : Atomic upsert (v15+)

Replaces the racy catch-23505-then-UPDATE pattern.

```sql
-- inventory.sku has a UNIQUE constraint
INSERT INTO inventory (sku, qty)
VALUES ('A-1', 10)
ON CONFLICT (sku)
DO UPDATE SET qty = inventory.qty + EXCLUDED.qty;
```

`inventory.qty` is the current stored value ; `EXCLUDED.qty` is the value from
the failed insert. The whole resolution happens inside one statement, atomic
even under concurrency.

## Example 2 : Idempotent insert with DO NOTHING (v15+)

```sql
-- re-running this is always a no-op, never an error
INSERT INTO tags (name) VALUES ('postgres')
ON CONFLICT (name) DO NOTHING;

-- get the id whether the row was inserted now or already existed :
WITH ins AS (
    INSERT INTO tags (name) VALUES ('postgres')
    ON CONFLICT (name) DO NOTHING
    RETURNING id
)
SELECT id FROM ins
UNION ALL
SELECT id FROM tags WHERE name = 'postgres'
LIMIT 1;
```

`DO NOTHING` does not `RETURNING` the existing row, so the `UNION ALL` fallback
fetches the id when no insert happened.

## Example 3 : Conditional upsert with WHERE (v15+)

```sql
-- only overwrite when the incoming row is newer
INSERT INTO documents AS d (id, body, updated_at)
VALUES (42, 'new text', now())
ON CONFLICT (id) DO UPDATE
    SET body = EXCLUDED.body, updated_at = EXCLUDED.updated_at
    WHERE EXCLUDED.updated_at > d.updated_at;
```

The `WHERE` is evaluated after the conflict is detected ; a stale incoming row
leaves the stored row untouched and raises no error.

## Example 4 : Upsert on a named constraint (v15+)

```sql
-- when inference is ambiguous, name the arbiter explicitly
INSERT INTO distributors (did, dname) VALUES (9, 'Antwerp Design')
ON CONFLICT ON CONSTRAINT distributors_pkey DO NOTHING;
```

## Example 5 : Index every foreign-key child column (v15+)

```sql
CREATE TABLE orders (
    id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    placed_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE order_items (
    id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_id  bigint NOT NULL REFERENCES orders (id) ON DELETE CASCADE,
    sku       text   NOT NULL,
    qty       int    NOT NULL CHECK (qty > 0)
);

-- REQUIRED : the FK does not create this. Without it, DELETE FROM orders
-- sequentially scans order_items for every deleted parent row.
CREATE INDEX ON order_items (order_id);
```

## Example 6 : Circular / self-referencing FK with deferral (v15+)

```sql
CREATE TABLE employees (
    id         int PRIMARY KEY,
    name       text NOT NULL,
    manager_id int
);

-- DEFERRABLE so a mutual pair can be inserted in one transaction
ALTER TABLE employees
  ADD CONSTRAINT employees_manager_fk
  FOREIGN KEY (manager_id) REFERENCES employees (id)
  DEFERRABLE INITIALLY DEFERRED;

BEGIN;
  INSERT INTO employees (id, name, manager_id) VALUES (1, 'Ada', 2);
  INSERT INTO employees (id, name, manager_id) VALUES (2, 'Bo', 1);
COMMIT;   -- both FK checks run here, both succeed
```

Under the default `NOT DEFERRABLE`, the first `INSERT` fails immediately with
`23503` because manager `2` does not exist yet.

## Example 7 : Defer an INITIALLY IMMEDIATE constraint per transaction (v15+)

```sql
ALTER TABLE accounts
  ADD CONSTRAINT accounts_parent_fk
  FOREIGN KEY (parent_id) REFERENCES accounts (id)
  DEFERRABLE INITIALLY IMMEDIATE;   -- normally checked per statement

BEGIN;
  SET CONSTRAINTS accounts_parent_fk DEFERRED;   -- defer just for this txn
  -- ... bulk insert in any order ...
COMMIT;   -- checked here
```

## Example 8 : Add a constraint to a large table without a blocking scan (v15+)

```sql
-- step 1 : add NOT VALID, brief lock, no scan of existing rows
ALTER TABLE orders
  ADD CONSTRAINT orders_total_nonneg CHECK (total >= 0) NOT VALID;

-- step 2 : off-peak, validate old rows under SHARE UPDATE EXCLUSIVE
ALTER TABLE orders VALIDATE CONSTRAINT orders_total_nonneg;
```

Same two-step approach for a foreign key :

```sql
ALTER TABLE orders
  ADD CONSTRAINT orders_customer_fk
  FOREIGN KEY (customer_id) REFERENCES customers (id) NOT VALID;

ALTER TABLE orders VALIDATE CONSTRAINT orders_customer_fk;
```

## Example 9 : EXCLUDE constraint preventing overlapping ranges (v15+)

```sql
CREATE EXTENSION IF NOT EXISTS btree_gist;

CREATE TABLE room_bookings (
    id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    room_id   int NOT NULL,
    during    tstzrange NOT NULL,
    -- no two bookings for the same room may overlap in time
    EXCLUDE USING gist (room_id WITH =, during WITH &&)
);

-- this second insert raises 23P01 exclusion_violation :
INSERT INTO room_bookings (room_id, during)
VALUES (1, '[2026-06-01 09:00, 2026-06-01 11:00)');
INSERT INTO room_bookings (room_id, during)
VALUES (1, '[2026-06-01 10:00, 2026-06-01 12:00)');  -- ERROR 23P01
```

`btree_gist` supplies the `=` operator class GiST needs for the `room_id`
equality part. Detect a conflict before inserting with `&&` against existing
rows, or use a deferred EXCLUDE constraint for bulk reshuffles.

## Example 10 : Typed error handling, psycopg 3 (Python)

```python
import psycopg
from psycopg import errors

def create_user(conn, email: str) -> int:
    try:
        with conn.transaction():
            row = conn.execute(
                "INSERT INTO users (email) VALUES (%s) RETURNING id", [email]
            ).fetchone()
            return row[0]
    except errors.UniqueViolation as e:
        # SQLSTATE 23505 : map to a domain error, do NOT retry blindly
        raise ValueError(f"email taken (constraint {e.diag.constraint_name})")
    except errors.ForeignKeyViolation as e:        # 23503
        raise ValueError(f"unknown reference: {e.diag.message_detail}")
    except errors.NotNullViolation as e:           # 23502
        raise ValueError(f"missing required field: {e.diag.column_name}")
    except errors.CheckViolation as e:             # 23514
        raise ValueError(f"invalid value: {e.diag.constraint_name}")
```

`e.diag.sqlstate`, `e.diag.constraint_name`, `e.diag.column_name`, and
`e.diag.message_detail` come straight from the server error fields.

## Example 11 : Typed error handling, node-postgres (JavaScript)

```javascript
const PG_ERR = {
  '23505': 'unique_violation',
  '23503': 'foreign_key_violation',
  '23502': 'not_null_violation',
  '23514': 'check_violation',
  '23P01': 'exclusion_violation',
};

try {
  await pool.query('INSERT INTO users(email) VALUES($1)', [email]);
} catch (err) {
  if (err.code === '23505') {
    // err.constraint -> 'users_email_key', err.detail -> 'Key (email)=(...)...'
    throw new Error(`email already registered: ${err.constraint}`);
  }
  if (PG_ERR[err.code]) throw new Error(`${PG_ERR[err.code]}: ${err.detail}`);
  throw err;
}
```

## Example 12 : Verify constraints and FK index coverage

```sql
-- list every constraint on a table
SELECT conname, contype, condeferrable, condeferred,
       pg_get_constraintdef(oid) AS definition
FROM pg_constraint
WHERE conrelid = 'public.order_items'::regclass
ORDER BY contype;

-- a NOT VALID constraint shows convalidated = false until VALIDATE runs
SELECT conname, convalidated
FROM pg_constraint
WHERE conrelid = 'public.orders'::regclass AND NOT convalidated;
```
