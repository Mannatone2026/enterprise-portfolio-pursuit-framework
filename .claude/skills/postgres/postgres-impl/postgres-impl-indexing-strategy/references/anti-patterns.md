# postgres-impl-indexing-strategy : Anti-Patterns

Each anti-pattern : cause, symptom, fix. Most indexing faults are **silent performance failures** , the query still returns correct rows, just slowly, with no error and no SQLSTATE. Verified against postgresql.org/docs/17 (Last Verified : 2026-05-19).

## 1. Missing index on a foreign-key column

CAUSE : a foreign key indexes the **parent** side (it must be unique) but PostgreSQL does NOT auto-create an index on the **child** column.
SYMPTOM : every `DELETE` or key-`UPDATE` on the parent does a Seq Scan of the whole child table to find referencing rows. `ON DELETE CASCADE` does this once per cascaded row. Joins on the FK fall back to Seq Scan.
SQLSTATE : none , a silent performance fault.
FIX : index every child-side FK column.

```sql
CREATE INDEX idx_order_items_order_id ON order_items (order_id);
```

## 2. Expression-index mismatch

CAUSE : the index stores a computed value (`lower(email)`) but the query filters the raw column (`email = $1`).
SYMPTOM : `EXPLAIN` shows a Seq Scan despite the index existing. The index is correct but never matched.
SQLSTATE : none.
FIX : write the query with the **identical** expression the index uses.

```sql
-- index :
CREATE INDEX idx_users_lower_email ON users ((lower(email)));
-- query MUST say :
SELECT * FROM users WHERE lower(email) = lower($1);
```

## 3. Redundant index covered by a multicolumn prefix

CAUSE : a single-column index on `(a)` exists alongside a multicolumn index on `(a, b, c)`. The leftmost prefix of `(a, b, c)` already serves every `(a)` query.
SYMPTOM : extra disk usage, slower INSERT/UPDATE (two indexes maintained where one suffices), no read benefit.
SQLSTATE : none.
FIX : drop the narrow index ; keep the multicolumn one.

```sql
DROP INDEX idx_orders_tenant_id;   -- (a) is the prefix of (a, b, c)
```

## 4. Plain B-tree on a low-cardinality boolean

CAUSE : a B-tree on a boolean / status flag indexes every row, but queries want only the rare value.
SYMPTOM : a large index that the planner often ignores in favour of a Seq Scan, because most rows match the common value.
SQLSTATE : none.
FIX : use a partial index covering only the selective rows.

```sql
-- instead of : CREATE INDEX ON jobs (processed);
CREATE INDEX idx_jobs_pending ON jobs (created_at) WHERE NOT processed;
```

## 5. Too many indexes

CAUSE : indexes added defensively, one per column, never audited.
SYMPTOM : every INSERT/UPDATE maintains every index, so write latency climbs ; HOT updates become impossible when any indexed column changes ; the table bloats faster.
SQLSTATE : none.
FIX : drop indexes with `idx_scan = 0` in `pg_stat_user_indexes` (keep unique indexes , they enforce constraints).

```sql
SELECT relname, indexrelname, idx_scan
FROM pg_stat_user_indexes WHERE idx_scan = 0;
```

## 6. Wrong access method for the operator

CAUSE : a plain B-tree on a `jsonb` or array column, expecting `@>` containment to be indexed.
SYMPTOM : `WHERE data @> '{"k":"v"}'` is a Seq Scan ; B-tree cannot index the containment operator at all.
SQLSTATE : none.
FIX : use GIN for `jsonb`, arrays, and full-text.

```sql
CREATE INDEX idx_docs_data ON docs USING GIN (data jsonb_path_ops);
```

## 7. Range column first in a multicolumn index

CAUSE : the multicolumn index leads with a range / inequality column, e.g. `(created_at, tenant_id)`.
SYMPTOM : `WHERE tenant_id = $1` cannot use the index (no leading `tenant_id`). `WHERE created_at >= $1 AND tenant_id = $2` stops probing after the range column, so `tenant_id` is filtered in memory.
SQLSTATE : none.
FIX : equality columns first, the single range / sort column last.

```sql
CREATE INDEX idx_orders_tenant_created ON orders (tenant_id, created_at);
```

## 8. Non-concurrent CREATE INDEX on a live table

CAUSE : `CREATE INDEX` (without `CONCURRENTLY`) takes an `ACCESS EXCLUSIVE` lock.
SYMPTOM : every read and write against the table blocks for the entire duration of the build , an outage on a large table.
SQLSTATE : none (the statement succeeds ; the blocking is the harm).
FIX : use `CREATE INDEX CONCURRENTLY` on any live, write-taking table.

```sql
CREATE INDEX CONCURRENTLY idx_orders_customer ON orders (customer_id);
```

## 9. Leftover INVALID index from a failed concurrent build

CAUSE : a `CREATE INDEX CONCURRENTLY` build failed (duplicate key, cancelled, crash). It leaves an `INVALID` index behind.
SYMPTOM : the index is ignored by queries but still incurs maintenance overhead on every write. For a unique index, the constraint is still enforced despite invalidity.
SQLSTATE : `25001` (`active_sql_transaction`) if `CONCURRENTLY` was attempted inside a transaction block , that is the most common cause of the failure.
FIX : drop the invalid index and retry, or `REINDEX ... CONCURRENTLY` (v12+).

```sql
SELECT indexrelid::regclass FROM pg_index WHERE NOT indisvalid;
DROP INDEX CONCURRENTLY idx_orders_customer;
```

## 10. Non-IMMUTABLE function in an index expression

CAUSE : an expression index uses a `STABLE` or `VOLATILE` function (`now()`, an unpinned timezone cast, a user function not marked `IMMUTABLE`).
SYMPTOM : `CREATE INDEX` fails immediately.
SQLSTATE : `42P17` (`invalid_object_definition`) , "functions in index expression must be marked IMMUTABLE".
FIX : use only `IMMUTABLE` functions ; mark a custom function `IMMUTABLE` only if it is genuinely deterministic.

```sql
-- fails : timezone-dependent cast is STABLE
-- CREATE INDEX ON events ((created_at::date));
-- works : the function is immutable
CREATE INDEX idx_events_day ON events ((date_trunc('day', created_at)));
```

## 11. BRIN on an uncorrelated column

CAUSE : BRIN created on a column whose values are scattered across the heap, with no physical-order correlation.
SYMPTOM : almost every block range overlaps the search value, so BRIN discards nothing and the query reads the whole table , while the index still costs maintenance.
SQLSTATE : none.
FIX : use BRIN only when the column correlates with physical row order (append-only insert-time columns). Otherwise use B-tree.

## 12. Implicit cast defeats the index

CAUSE : the query compares a column to a literal of a different type, e.g. `WHERE int_col = '123'` or `WHERE varchar_col = 123`.
SYMPTOM : the planner casts every row instead of the parameter, so the index on the column cannot be used , Seq Scan.
SQLSTATE : none.
FIX : make the literal / parameter type match the column type, or build a matching expression index.

```sql
-- defeats the index :   WHERE varchar_col = 123
-- uses the index :      WHERE varchar_col = '123'
```

## 13. No ANALYZE after a partial or expression index

CAUSE : a partial or expression index is created but `ANALYZE` is not run.
SYMPTOM : the planner has no statistics on the indexed subset or computed values, estimates selectivity poorly, and avoids the new index.
SQLSTATE : none.
FIX : run `ANALYZE` (or `VACUUM ANALYZE`) on the table after creating a partial or expression index.

```sql
CREATE INDEX idx_users_lower_email ON users ((lower(email)));
ANALYZE users;
```

## 14. Index-only scan with high Heap Fetches

CAUSE : an `INCLUDE` covering index exists, but VACUUM has not run, so the visibility map is stale.
SYMPTOM : `EXPLAIN` shows `Index Only Scan` but with `Heap Fetches: <large>` , the scan is still hitting the heap for visibility checks.
SQLSTATE : none.
FIX : run `VACUUM` on the table so the visibility map marks pages all-visible ; ensure autovacuum keeps up.

```sql
VACUUM films;   -- refreshes the visibility map so index-only scans skip the heap
```

## Source URLs

- https://www.postgresql.org/docs/17/indexes-types.html
- https://www.postgresql.org/docs/17/indexes-partial.html
- https://www.postgresql.org/docs/17/indexes-expressional.html
- https://www.postgresql.org/docs/17/indexes-index-only-scans.html
- https://www.postgresql.org/docs/17/sql-createindex.html
- https://www.postgresql.org/docs/17/ddl-constraints.html
- https://www.postgresql.org/docs/17/errcodes-appendix.html
