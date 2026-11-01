# methods : zero-downtime migration primitives

API surface for postgres-impl-zero-downtime-migrations. Verified against
PostgreSQL 17 official documentation (sql-altertable.html,
sql-createindex.html). Behaviour is identical on 15, 16, and 17 unless a
version annotation says otherwise.

## ALTER TABLE : rewrite and lock matrix

A table rewrite is the expensive part : it rebuilds every page (and every
index) under `ACCESS EXCLUSIVE`. But even a metadata-only form still
requests `ACCESS EXCLUSIVE` briefly, so the lock queue (see below) applies
to every `ALTER TABLE`.

| Operation | Table rewrite? | Lock level |
|-----------|----------------|------------|
| `ADD COLUMN` (no DEFAULT) | No | ACCESS EXCLUSIVE |
| `ADD COLUMN` (non-volatile DEFAULT) | No | ACCESS EXCLUSIVE |
| `ADD COLUMN` (volatile DEFAULT) | **Yes** | ACCESS EXCLUSIVE |
| `DROP COLUMN` | No (space reclaimed lazily) | ACCESS EXCLUSIVE |
| `RENAME COLUMN` / `RENAME` / `RENAME CONSTRAINT` | No | ACCESS EXCLUSIVE |
| `SET DEFAULT` / `DROP DEFAULT` | No | ACCESS EXCLUSIVE |
| `ALTER COLUMN TYPE` | **Yes** (almost always) | ACCESS EXCLUSIVE |
| `SET NOT NULL` | No, but scans the table | ACCESS EXCLUSIVE |
| `DROP NOT NULL` | No | ACCESS EXCLUSIVE |
| `ADD CHECK` constraint (plain) | No, but scans the table | ACCESS EXCLUSIVE |
| `ADD CONSTRAINT ... NOT VALID` (CHECK or FK) | No, no scan | ACCESS EXCLUSIVE (brief, metadata) |
| `VALIDATE CONSTRAINT` | No, scans the table | SHARE UPDATE EXCLUSIVE |
| `ADD FOREIGN KEY` (plain) | No, but scans the table | SHARE ROW EXCLUSIVE (+ SHARE ROW EXCLUSIVE on referenced table) |
| `SET STATISTICS` / `ALTER COLUMN SET STATISTICS` | No | SHARE UPDATE EXCLUSIVE |
| `SET (fillfactor = ...)` and other storage params | No | SHARE UPDATE EXCLUSIVE |
| `CLUSTER ON` / `SET WITHOUT CLUSTER` | No | SHARE UPDATE EXCLUSIVE |
| `ATTACH PARTITION` | No (scans new partition unless a proving CHECK exists) | SHARE UPDATE EXCLUSIVE on parent + ACCESS EXCLUSIVE on partition |
| `DETACH PARTITION CONCURRENTLY` | No | Reduced, two-phase (v14+) |

### ALTER COLUMN TYPE : the no-rewrite exception

> "when changing the type of an existing column, if the `USING` clause does
> not change the column contents and the old type is either binary coercible
> to the new type or an unconstrained domain over the new type, a table
> rewrite is not needed."

Even then, "indexes must always be rebuilt unless the system can verify that
the new index would be logically equivalent to the existing one." Treat
`ALTER COLUMN TYPE` as a rewrite unless this exact exception is proven.

### ADD COLUMN with DEFAULT

> "When a column is added with `ADD COLUMN` and a non-volatile `DEFAULT` is
> specified, the default is evaluated at the time of the statement and the
> result stored in the table's metadata. That value will be used for the
> column for all existing rows. If no `DEFAULT` is specified, NULL is used.
> In neither case is a rewrite of the table required."

> "Adding a column with a volatile `DEFAULT` ... will require the entire
> table and its indexes to be rewritten."

The non-volatile-default fast path is available in every supported version
(15, 16, 17).

### NOT VALID and VALIDATE CONSTRAINT

`NOT VALID` is allowed only for foreign key and `CHECK` constraints. With
`NOT VALID`, the verification scan is skipped, but the constraint is still
enforced for every subsequent insert and update. `VALIDATE CONSTRAINT` later
scans existing rows under `SHARE UPDATE EXCLUSIVE` (plus a `ROW SHARE` lock
on the referenced table for a foreign key). Nothing happens if the
constraint is already valid.

### SET NOT NULL scan-skip

`SET NOT NULL` normally scans the whole table to confirm no NULLs. If a
valid `CHECK` constraint that proves the column cannot be NULL exists, and
is not dropped in the same command, the scan is skipped.

## The lock queue

Lock requests on a relation are granted roughly in arrival order. A pending
`ACCESS EXCLUSIVE` request that cannot be granted (because a long query
holds a weaker lock) blocks every later request, including plain `SELECT`s.
Consequences :

- A metadata-only `ALTER TABLE` behind one slow query freezes the table.
- `lock_timeout` bounds how long the `ALTER` waits before giving up.
- `statement_timeout` bounds how long the `ALTER` runs once it has the lock.

```sql
SET lock_timeout = '3s';        -- give up waiting for the lock after 3s
SET statement_timeout = '60s';  -- abort the operation if it runs too long
```

`lock_timeout` defaults to `0` (wait forever). A migration runner sets it,
runs the DDL, and retries on SQLSTATE `55P03` (`lock_not_available`).

## CREATE INDEX CONCURRENTLY

| Aspect | Plain CREATE INDEX | CREATE INDEX CONCURRENTLY |
|--------|--------------------|---------------------------|
| Writes during build | Blocked | Allowed |
| Lock | Locks table against writes | SHARE UPDATE EXCLUSIVE |
| Table scans | One | Two, plus waits for transactions |
| Wall-clock time | Shorter | Significantly longer |
| Runs inside a transaction block | Yes | **No** |
| Concurrent builds per table | Many | One at a time |

Failure (deadlock, unique violation) leaves an `INVALID` index :

> "the `CREATE INDEX` command will fail but leave behind an 'invalid' index.
> This index will be ignored for querying purposes because it might be
> incomplete; however it will still consume update overhead."

Detect : `\d table` shows the index as `INVALID`; or query
`pg_index.indisvalid = false`. Recover : `DROP INDEX [CONCURRENTLY]` and
retry, or `REINDEX INDEX CONCURRENTLY` (v12+).

Unique index caveat : during a concurrent unique index build, the uniqueness
constraint is enforced against other transactions once the second scan
begins, so violations can surface in other queries before the index is
ready, or even when the build itself eventually fails.

Partitioned tables : a concurrent build is not supported on the partitioned
table directly. Build the index on each partition concurrently, then create
the partitioned index non-concurrently.

## REINDEX CONCURRENTLY (v12+)

`REINDEX INDEX CONCURRENTLY name` and `REINDEX TABLE CONCURRENTLY name`
rebuild indexes with a reduced lock that allows writes. Used to remove index
bloat and to rebuild an `INVALID` index left by a failed concurrent build.
Like `CREATE INDEX CONCURRENTLY`, it cannot run inside a transaction block.

## DETACH PARTITION CONCURRENTLY (v14+)

Runs in two internal transactions with a reduced lock so other sessions can
keep using the partitioned table. Cannot run inside a transaction block and
is not allowed when the partitioned table has a default partition.

## Index fillfactor

The B-tree `fillfactor` storage parameter defaults to 90 (range 10-100).
Lower it for tables with heavy inserts/updates to smooth out page splits;
raise it toward 100 only for tables that are completely static.

## SQLSTATE codes

| SQLSTATE | Name | When |
|----------|------|------|
| `55P03` | `lock_not_available` | `lock_timeout` exceeded waiting for a lock. |
| `40P01` | `deadlock_detected` | Deadlock during a concurrent build or migration. |
| `23505` | `unique_violation` | Duplicate surfaced during a concurrent unique index build. |
| `57014` | `query_canceled` | `statement_timeout` aborted the operation. |

## Sources

- https://www.postgresql.org/docs/17/sql-altertable.html
- https://www.postgresql.org/docs/17/sql-createindex.html
- https://www.postgresql.org/docs/17/sql-reindex.html
- https://www.postgresql.org/docs/17/runtime-config-client.html
- https://www.postgresql.org/docs/17/errcodes-appendix.html
