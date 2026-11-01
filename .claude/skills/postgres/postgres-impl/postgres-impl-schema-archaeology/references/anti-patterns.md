# Schema Archaeology : Anti-Patterns

Each entry : cause, symptom, SQLSTATE where applicable, and the fix pattern.
Verified against PostgreSQL 17 catalog and statistics documentation.

## AP-1 : information_schema for performance-sensitive introspection

CAUSE : an audit script or admin tool queries `information_schema.columns`,
`information_schema.tables`, or `information_schema.table_constraints` in a hot
path or against a database with thousands of objects.

SYMPTOM : the introspection query is slow, sometimes seconds, while the
equivalent `pg_catalog` query returns instantly. The `information_schema` views
are SQL-standard wrappers built from multi-way joins over the same catalogs plus
per-row permission filtering.

FIX : use `pg_catalog` directly for all introspection inside PostgreSQL. Reserve
`information_schema` for queries that must also run on another SQL vendor.

```sql
-- SLOW : standard view, extra joins and permission filtering
SELECT table_name FROM information_schema.tables WHERE table_schema = 'public';

-- FAST : direct catalog access
SELECT c.relname FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind = 'r';
```

## AP-2 : DROP CASCADE without checking pg_depend

CAUSE : a `DROP TABLE` fails because objects depend on it, and the operator
re-runs it with `CASCADE` to make the error go away.

SYMPTOM : without `CASCADE` the statement fails with SQLSTATE 2BP01
`dependent_objects_still_exist` and lists every dependent object. With `CASCADE`
it succeeds silently and also drops views, foreign keys, functions, and triggers
the operator never intended to remove.

FIX : the `RESTRICT` failure list IS the dependency report. Read it first. Or
query `pg_depend` for `refobjid = target` rows. Only run `CASCADE` after
deliberately reviewing the dependent objects.

```sql
-- The error from a plain DROP is the information you need :
DROP TABLE customers;            -- fails, lists dependents (SQLSTATE 2BP01)

-- Inspect explicitly before deciding :
SELECT pg_describe_object(classid, objid, objsubid)
FROM pg_depend WHERE refobjid = 'customers'::regclass AND deptype = 'n';
```

## AP-3 : Dropping a never-scanned index that enforces a constraint

CAUSE : an "unused index" report based on `idx_scan = 0` includes the index
backing a `PRIMARY KEY` or `UNIQUE` constraint, and that index is dropped.

SYMPTOM : `idx_scan` can legitimately be 0 for a unique index that is never used
for lookups but still enforces uniqueness on every write. Dropping it removes the
constraint and lets duplicate rows in.

FIX : always exclude `indisunique` and `indisprimary` indexes from unused-index
cleanup. To remove the index, drop the constraint with `ALTER TABLE ... DROP
CONSTRAINT` instead, and only when uniqueness is genuinely no longer required.

```sql
SELECT s.relname, s.indexrelname
FROM pg_stat_user_indexes s
JOIN pg_index i ON i.indexrelid = s.indexrelid
WHERE s.idx_scan = 0
  AND NOT i.indisunique          -- mandatory guard
  AND NOT i.indisprimary;
```

## AP-4 : Reading idx_scan = 0 right after a statistics reset

CAUSE : an unused-index verdict is made from `pg_stat_user_indexes.idx_scan`
shortly after `pg_stat_reset()` was called, or after a crash recovery, or on a
freshly promoted replica.

SYMPTOM : indexes that are heavily used in production show `idx_scan = 0` because
the counter window only covers minutes, not a full business cycle.

FIX : confirm the statistics age before trusting a zero count. Check
`pg_stat_user_tables` timestamps and, on v16+, `last_idx_scan`. An index is only
safely "unused" if it has been zero across a representative period (weekly and
monthly jobs included).

```sql
-- v16+ : a zero count is meaningful only with an old enough window
SELECT relname, indexrelname, idx_scan, last_idx_scan
FROM pg_stat_user_indexes
WHERE idx_scan = 0;
-- Cross-check the stats reset time before acting :
SELECT stats_reset FROM pg_stat_database WHERE datname = current_database();
```

## AP-5 : Trusting physical column order

CAUSE : code assumes `pg_attribute.attnum` values are contiguous, or relies on
`SELECT *` column order matching a remembered layout.

SYMPTOM : a dropped column leaves a permanent gap in `attnum` (the row stays with
`attisdropped = true`). Queries that index result columns by position, or `COPY`
statements without an explicit column list, break after an unrelated
`ALTER TABLE ... DROP COLUMN`.

FIX : never rely on physical position. Always filter
`attnum > 0 AND NOT attisdropped` when listing columns, and always name columns
explicitly in `SELECT`, `INSERT`, and `COPY`.

```sql
-- Live user columns only, gaps and dropped slots excluded :
SELECT attname FROM pg_attribute
WHERE attrelid = 'orders'::regclass
  AND attnum > 0 AND NOT attisdropped
ORDER BY attnum;
```

## AP-6 : Ignoring system columns during archaeology

CAUSE : an audit assumes the columns shown by `\d` or `information_schema.columns`
are all the columns a table has.

SYMPTOM : the hidden system columns `ctid`, `xmin`, `xmax`, `cmin`, `cmax`,
`tableoid` are never listed (their `attnum` is negative) yet they carry real
information : `ctid` is the physical row locator used in chunked-update patterns,
`xmin`/`xmax` reveal MVCC visibility and bloat. An archaeology pass that ignores
them misses tools and signals.

FIX : know that system columns exist and query them by name when needed. They are
real and selectable even though no catalog browse lists them as ordinary columns.

```sql
-- ctid drives the chunked-backfill pattern; xmin/xmax reveal MVCC state
SELECT ctid, xmin, xmax, * FROM orders LIMIT 5;
```

## AP-7 : Assuming referential integrity on an unvalidated FK

CAUSE : an FK constraint exists in `pg_constraint`, so the schema looks sound, but
the constraint was added `NOT VALID` and never validated.

SYMPTOM : orphan child rows exist even though a foreign-key constraint is present.
A `NOT VALID` FK enforces new writes but never checked the rows that predate it.

FIX : check `pg_constraint.convalidated`. For any FK where it is false, either run
`ALTER TABLE ... VALIDATE CONSTRAINT` (which scans for violations) or hunt the
orphans with a `LEFT JOIN` first.

```sql
-- Find FKs that were never validated :
SELECT conrelid::regclass AS table_name, conname
FROM pg_constraint
WHERE contype = 'f' AND NOT convalidated;
```
