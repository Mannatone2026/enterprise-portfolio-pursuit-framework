# UPSERT and MERGE Anti-Patterns Reference

Ten documented bugs in `INSERT ... ON CONFLICT` and `MERGE` code with reproducible failure SQL and the official fix. Sources verified 2026-05-19 against `postgresql.org/docs/17/sql-insert.html` and `postgresql.org/docs/17/sql-merge.html`.

## 1. ON CONFLICT Without Matching Unique Index :

**Symptom** :

```
ERROR:  there is no unique or exclusion constraint matching the ON CONFLICT specification
SQLSTATE 42P10
```

**Reproducer** :

```sql
CREATE TABLE t (id bigserial PRIMARY KEY, email text);
INSERT INTO t (email) VALUES ('a@b.c')
ON CONFLICT (email) DO NOTHING;        -- email has no UNIQUE index
```

**Fix** :

```sql
CREATE UNIQUE INDEX ON t (email);
INSERT INTO t (email) VALUES ('a@b.c') ON CONFLICT (email) DO NOTHING;
```

**Reference** : postgresql.org/docs/17/sql-insert.html § "ON CONFLICT Clause".

## 2. MERGE With Non-Deterministic Source :

**Symptom** :

```
ERROR:  MERGE command cannot affect row a second time
HINT:   Ensure that not more than one source row matches any one target row.
SQLSTATE 21000
```

**Reproducer** :

```sql
CREATE TABLE users (id bigint PRIMARY KEY, last_seen timestamptz);
CREATE TABLE events (user_id bigint, ts timestamptz);
INSERT INTO events VALUES (1, '2026-01-01'), (1, '2026-02-01');  -- two events for user 1

MERGE INTO users t USING events s ON t.id = s.user_id
WHEN MATCHED THEN UPDATE SET last_seen = s.ts;
-- ERROR: MERGE command cannot affect row a second time
```

**Fix** : deduplicate the source. Either `DISTINCT ON` (full row) or `GROUP BY` (aggregate) :

```sql
MERGE INTO users t USING (
  SELECT DISTINCT ON (user_id) user_id, ts FROM events ORDER BY user_id, ts DESC
) s ON t.id = s.user_id
WHEN MATCHED THEN UPDATE SET last_seen = s.ts;
```

**Reference** : postgresql.org/docs/17/sql-merge.html § "Notes".

## 3. Using MERGE For Simple Upsert :

**Symptom** : 5-line MERGE where 2-line ON CONFLICT would do. Harder to read, harder to grep, and on v15/v16 you cannot use RETURNING.

**Reproducer** :

```sql
MERGE INTO t USING (VALUES (1, 'a@b.c')) AS s(id, email) ON t.id = s.id
WHEN MATCHED THEN UPDATE SET email = s.email
WHEN NOT MATCHED THEN INSERT (id, email) VALUES (s.id, s.email);
```

**Fix** :

```sql
INSERT INTO t (id, email) VALUES (1, 'a@b.c')
ON CONFLICT (id) DO UPDATE SET email = EXCLUDED.email;
```

**Rule of thumb** : single-conflict-target, single action → UPSERT. Multi-action sync → MERGE.

## 4. EXCLUDED Reference Mistake :

**Symptom** : UPSERT silently does the wrong thing — updates a column to its own existing value.

**Reproducer** :

```sql
INSERT INTO users (id, name) VALUES (1, 'Alice 2')
ON CONFLICT (id) DO UPDATE SET name = users.name;   -- no-op : sets name to itself
```

**Fix** :

```sql
INSERT INTO users (id, name) VALUES (1, 'Alice 2')
ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name;   -- applies the proposed value
```

**Mnemonic** : `EXCLUDED.col` = the new value the INSERT tried to write. Table-qualified `col` (or alias) = the existing value already in the row.

## 5. Partial-Unique-Index Predicate Mismatch :

**Symptom** : `42P10` even though a unique index exists.

**Reproducer** :

```sql
CREATE UNIQUE INDEX ON users (email) WHERE is_active;
INSERT INTO users (email, is_active) VALUES ('a@b.c', true)
ON CONFLICT (email) DO NOTHING;        -- missing WHERE clause
-- ERROR: there is no unique or exclusion constraint matching the ON CONFLICT specification
```

**Fix** : repeat the partial-index predicate verbatim :

```sql
INSERT INTO users (email, is_active) VALUES ('a@b.c', true)
ON CONFLICT (email) WHERE is_active DO NOTHING;
```

**Reference** : postgresql.org/docs/17/sql-insert.html § "Partial Indexes".

## 6. RETURNING Expected From DO NOTHING :

**Symptom** : RETURNING produces fewer rows than expected; rows that "should have been" upserted are missing.

**Reproducer** :

```sql
INSERT INTO users (id, name) VALUES (1, 'A'), (2, 'B')
ON CONFLICT (id) DO NOTHING
RETURNING id;
-- If id=1 already exists, this returns only id=2.
```

**Fix** : DO NOTHING rows are intentionally excluded from RETURNING. If you need ALL row IDs (including unchanged), use `DO UPDATE SET col = col` (still returns the row) or query separately :

```sql
INSERT INTO users (id, name) VALUES (1, 'A'), (2, 'B')
ON CONFLICT (id) DO UPDATE SET name = users.name   -- self-assign forces RETURNING to include
RETURNING id;
```

Note : self-assign still bumps `xmax`, so use only when you need the RETURNING surface.

## 7. MERGE RETURNING On v15/v16 :

**Symptom** :

```
ERROR:  syntax error at or near "RETURNING"
```

**Reproducer** : on PostgreSQL 15 or 16 :

```sql
MERGE INTO t USING s ON t.id = s.id
WHEN MATCHED THEN UPDATE SET v = s.v
RETURNING *;
-- ERROR on v15/v16. RETURNING from MERGE is v17+.
```

**Fix** : upgrade to v17, or split into a separate SELECT for audit, or use `INSERT ... ON CONFLICT ... RETURNING` if the use case fits :

```sql
WITH affected AS (
  INSERT INTO t (id, v) VALUES (1, 'x')
  ON CONFLICT (id) DO UPDATE SET v = EXCLUDED.v
  RETURNING id, v
)
SELECT * FROM affected;
```

**Reference** : postgresql.org/docs/release/17.0/ § "RETURNING for MERGE".

## 8. MERGE On Materialized View Or Foreign Table :

**Symptom** :

```
ERROR:  cannot execute MERGE on relation "..."
DETAIL: This operation is not supported for materialized views.
SQLSTATE 0A000
```

**Reproducer** :

```sql
CREATE MATERIALIZED VIEW mv AS SELECT id, val FROM base;
MERGE INTO mv t USING src s ON t.id = s.id WHEN MATCHED THEN UPDATE SET val = s.val;
```

**Fix** : MERGE the base table and `REFRESH MATERIALIZED VIEW mv` (or `REFRESH MATERIALIZED VIEW CONCURRENTLY mv` if a unique index exists). For foreign tables, write to the remote via a regular UPDATE/INSERT.

**Reference** : postgresql.org/docs/17/sql-merge.html § "Restrictions".

## 9. WHEN Clause Ordering Wrong :

**Symptom** : a `WHEN MATCHED THEN UPDATE` runs against rows you intended to DELETE; the more-specific WHEN never fires.

**Reproducer** :

```sql
MERGE INTO t USING s ON t.id = s.id
WHEN MATCHED THEN UPDATE SET v = s.v            -- catches everything first
WHEN MATCHED AND s.tombstone THEN DELETE;       -- never reached
```

**Fix** : put the more-specific clause first :

```sql
MERGE INTO t USING s ON t.id = s.id
WHEN MATCHED AND s.tombstone THEN DELETE
WHEN MATCHED THEN UPDATE SET v = s.v;
```

**Rule** : WHEN clauses are evaluated top-to-bottom; first match wins per source row.

## 10. Deadlocks On Concurrent MERGE :

**Symptom** :

```
ERROR:  deadlock detected
DETAIL: Process A waits for ShareLock on transaction X ; blocked by process B.
SQLSTATE 40P01
```

**Reproducer** : two concurrent MERGEs from sources that touch overlapping target rows in different orders :

```sql
-- Session 1
MERGE INTO accounts t USING source_a s ON t.id = s.id WHEN MATCHED THEN UPDATE SET v = s.v;
-- Session 2 (concurrent)
MERGE INTO accounts t USING source_b s ON t.id = s.id WHEN MATCHED THEN UPDATE SET v = s.v;
```

**Fix** : enforce a fixed scan order on every MERGE by sorting the source :

```sql
MERGE INTO accounts t USING (SELECT * FROM source_a ORDER BY id) s
ON t.id = s.id WHEN MATCHED THEN UPDATE SET v = s.v;
```

Alternative : serialize via `pg_advisory_xact_lock('table-merge'::regclass::int)` at the start of the transaction. Trades concurrency for correctness, acceptable for low-frequency batches.

**Reference** : postgresql.org/docs/17/explicit-locking.html § "Deadlocks".
