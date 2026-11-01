---
name: postgres-impl-indexing-strategy
description: >
  Use when a query is slow, picking which index type to create, or auditing a table for missing and redundant indexes.
  Prevents missing FK-column indexes (slow joins and cascade deletes), expression-index mismatch (index never used), redundant indexes bloating writes, and reaching for B-tree where GIN/GiST/BRIN fits.
  Covers B-tree / GIN / GiST / SP-GiST / BRIN / Hash decision tree, partial index, expression index, multicolumn column-ordering, INCLUDE covering columns (v11+), index-only scans, CREATE INDEX CONCURRENTLY.
  Keywords: index, B-tree, GIN, GiST, SP-GiST, BRIN, Hash, partial index, expression index, covering index, INCLUDE, multicolumn index, CREATE INDEX CONCURRENTLY, query is slow, which index should I use, missing index on foreign key, index not being used, too many indexes
license: MIT
compatibility: "Designed for Claude Code. Requires PostgreSQL 15, 16, or 17."
metadata:
  author: OpenAEC-Foundation
  version: "1.0"
---

# postgres-impl-indexing-strategy

## Quick Reference :

PostgreSQL ships six built-in index access methods. The choice is not stylistic : the wrong access method makes a query **unindexable** for the operator it uses. Pick the method by the operator class the query needs, not by habit.

- **B-tree** (default) : equality + range + `BETWEEN`/`IN` + `IS NULL` + `LIKE 'prefix%'` + sorted output. The ONLY method that backs `UNIQUE`, primary keys, and `ORDER BY` sort-avoidance.
- **GIN** : multi-value columns , `jsonb`, arrays, full-text `tsvector`, `pg_trgm`. Fast lookup, slow update.
- **GiST** : ranges, geometry/PostGIS, exclusion constraints, k-nearest-neighbour (`ORDER BY col <-> point`), FTS. Updatable.
- **SP-GiST** : non-balanced data , quadtrees, k-d trees, IP-prefix hierarchies, text-prefix. Single-column only.
- **BRIN** : very large tables whose physical row order correlates with the column (append-only time-series). Tiny on disk, block-range summaries.
- **Hash** : equality only. WAL-logged + replicated since v10. Rarely beats B-tree , default to B-tree unless measured.

Three orthogonal modifiers apply on top of the method : **partial** (`WHERE predicate` , index a row subset), **expression** (`(lower(email))` , index a computed value), and **INCLUDE** (v11+ , non-key payload columns for index-only scans). Build production indexes with `CREATE INDEX CONCURRENTLY` to avoid an `ACCESS EXCLUSIVE` lock.

ALWAYS index every foreign-key column on the child side. ALWAYS make an expression index match the query expression byte-for-byte. NEVER create an index whose key columns are already a prefix of another index.

## When To Use This Skill :

ALWAYS use this skill when :
- A query is slow and you need to decide which index to add
- Choosing between B-tree, GIN, GiST, SP-GiST, BRIN, or Hash
- Auditing a table for missing FK indexes or redundant indexes
- An index exists but `EXPLAIN` shows a Seq Scan anyway
- Adding an index to a live production table without downtime

NEVER use this skill for :
- Reading and interpreting `EXPLAIN` plan output : see `postgres-impl-explain-analyze`
- JSONB operator selection (`@>` vs `?`) and `jsonb_ops` vs `jsonb_path_ops` : see `postgres-syntax-jsonb`
- VACUUM, bloat repair, and `REINDEX` for wraparound : see `postgres-impl-vacuum-bloat`
- PostGIS spatial-index specifics : see the PostGIS impl skill

## Decision Trees :

### Which index access method? :

```
What does the query filter / sort on?
├── = , <, <=, >, >=, BETWEEN, IN, IS NULL, LIKE 'prefix%', ORDER BY :
│       B-tree   (also the only method for UNIQUE / primary key)
├── jsonb @> / ?, array @> / &&, full-text @@, pg_trgm LIKE '%mid%' :
│       GIN      (jsonb_path_ops for @>-only ; jsonb_ops for key-existence)
├── range && (overlap), exclusion constraint, geometry, k-NN (col <-> x) :
│       GiST
├── non-balanced hierarchy : IP-prefix (inet), quadtree points, text-prefix :
│       SP-GiST  (single-column only)
├── huge append-only table, column correlates with physical row order
│   (insert-time timestamp on an event log) :
│       BRIN     (tiny index ; useless if rows are not physically ordered)
└── pure equality, measured win over B-tree :
        Hash     (otherwise use B-tree , B-tree also serves equality)
```

### Plain, partial, expression, or covering? :

```
Start from a B-tree on the filtered column, then layer modifiers :
├── Query always filters a fixed subset (WHERE deleted_at IS NULL) :
│       PARTIAL  , CREATE INDEX ... WHERE deleted_at IS NULL
├── Query filters a computed value (WHERE lower(email) = $1) :
│       EXPRESSION , CREATE INDEX ... ((lower(email)))
├── Query reads a few extra columns the index could carry :
│       INCLUDE  , CREATE INDEX ... (key) INCLUDE (payload)  -- index-only scan
└── Query filters several columns together :
        MULTICOLUMN , see next tree for column order
```

### Multicolumn column ordering :

```
Index on (a, b, c) serves leftmost-prefix queries ONLY :
  WHERE a = ?                      uses index
  WHERE a = ? AND b = ?            uses index
  WHERE a = ? AND b = ? AND c = ?  uses index
  WHERE b = ?                      does NOT use index (no leading a)

Ordering rule :
  1. Equality-predicate columns FIRST (most selective equality leftmost)
  2. ONE range / inequality / sort column LAST
  A range predicate stops the index from probing columns to its right.
```

### Plain CREATE INDEX or CONCURRENTLY? :

```
Is the table live and taking writes?
├── Empty table, migration, maintenance window, inside a transaction :
│       CREATE INDEX            (ACCESS EXCLUSIVE lock ; one fast scan)
└── Live production table, cannot block writes :
        CREATE INDEX CONCURRENTLY   (SHARE UPDATE EXCLUSIVE ; two scans ;
                                     CANNOT run inside a transaction block)
```

## Patterns :

### Pattern 1 : Multicolumn B-tree with equality columns first

ALWAYS put equality-predicate columns before the range / sort column in a multicolumn index.
NEVER lead a multicolumn index with a range column , it blocks every column to its right.

```sql
-- Query : WHERE tenant_id = $1 AND status = $2 AND created_at >= $3 ORDER BY created_at
CREATE INDEX idx_orders_tenant_status_created
    ON orders (tenant_id, status, created_at);
```

WHY : the planner walks a B-tree left to right. `tenant_id` and `status` are equality predicates , they narrow the scan to a contiguous index range. `created_at` is a range predicate, so it must come last : once a range predicate is applied, columns further right can no longer be used for index probing. Putting `created_at` first would force a full-index scan filtered in memory. The trailing `created_at` also satisfies the `ORDER BY`, avoiding a Sort node. Source : postgresql.org/docs/17/indexes-multicolumn.html.

### Pattern 2 : Partial index for a sparse or low-cardinality predicate

ALWAYS use a partial index when queries only ever touch a fixed subset of rows, or when a column is low-cardinality (boolean, status flag).
NEVER build a plain index on a low-cardinality boolean , it indexes every row to serve a query that wants a fraction of them.

```sql
-- Soft-delete : queries always carry WHERE deleted_at IS NULL
CREATE INDEX idx_users_active_email
    ON users (email) WHERE deleted_at IS NULL;

-- Low-cardinality flag : index only the rare 'true' rows
CREATE INDEX idx_jobs_pending
    ON jobs (created_at) WHERE NOT processed;
```

WHY : a partial index is smaller, cheaper to maintain, and skips write overhead for rows outside the predicate. Critical caveat : the planner uses a partial index ONLY when the query's `WHERE` clause **mathematically implies** the index predicate. A parameterised query `WHERE x < $1` cannot match a `WHERE x < 100` index , the planner cannot prove `$1 <= 100` at plan time. Source : postgresql.org/docs/17/indexes-partial.html.

### Pattern 3 : Expression index that matches the query byte-for-byte

ALWAYS create the expression index with the exact expression the query uses, and only with `IMMUTABLE` functions.
NEVER assume `CREATE INDEX ON users ((lower(email)))` helps `WHERE email = $1` , the query must say `WHERE lower(email) = lower($1)`.

```sql
CREATE INDEX idx_users_lower_email ON users ((lower(email)));

-- Uses the index :
SELECT * FROM users WHERE lower(email) = lower($1);

-- Does NOT use the index , expression mismatch, plain Seq Scan :
SELECT * FROM users WHERE email = $1;
```

WHY : an expression index stores the result of the expression, not the raw column. The planner matches it only when the query contains the **identical** expression. A non-`IMMUTABLE` function in the index (e.g. `now()`, an unpinned `timezone` cast) is rejected with SQLSTATE `42P17`. Run `ANALYZE` after creating an expression or partial index , the planner needs fresh statistics on the computed values. Source : postgresql.org/docs/17/indexes-expressional.html.

### Pattern 4 : GIN index for JSONB and array columns

ALWAYS use GIN for `jsonb` containment, array overlap, and full-text search , B-tree cannot index these operators at all.
NEVER put a plain B-tree on a `jsonb` or array column expecting `@>` to be indexed , it never will be.

```sql
-- jsonb containment : pick the operator class by query shape
CREATE INDEX idx_docs_data ON docs USING GIN (data jsonb_path_ops);  -- @> only, smaller
CREATE INDEX idx_docs_data_full ON docs USING GIN (data);            -- jsonb_ops : also ? ?| ?&

-- array overlap / containment
CREATE INDEX idx_posts_tags ON posts USING GIN (tags);
```

WHY : GIN (Generalized Inverted Index) decomposes a multi-value column into individual indexable entries , every key/value for `jsonb`, every element for arrays. `jsonb_path_ops` indexes only key+value paths as a single hash : smaller and faster for `@>`, but it cannot serve key-existence operators `?`, `?|`, `?&`. GIN updates are slow (the `fastupdate` pending list batches them) , GIN is a fast-lookup / slow-write tradeoff. Source : postgresql.org/docs/17/gin.html, postgresql.org/docs/17/datatype-json.html.

### Pattern 5 : GiST for ranges, exclusion constraints, and k-NN

ALWAYS use GiST for range-overlap (`&&`), geometry, and nearest-neighbour ordering , GiST is the only general method that backs exclusion constraints.
NEVER try to enforce "no overlapping bookings" with a `UNIQUE` constraint , `UNIQUE` tests equality, not overlap.

```sql
-- Exclusion constraint : no two reservations of the same room overlap in time
CREATE TABLE reservation (
    room    text,
    during  tsrange,
    EXCLUDE USING GIST (room WITH =, during WITH &&)
);

-- k-nearest-neighbour : GiST optimizes ORDER BY <distance>
CREATE INDEX idx_places_geom ON places USING GIST (geom);
SELECT * FROM places ORDER BY geom <-> ST_Point(5, 52) LIMIT 10;
```

WHY : GiST is an extensible balanced-tree framework. An exclusion constraint generalises `UNIQUE` : it forbids any pair of rows where the listed operators all return true (`room` equal AND `during` overlapping). GiST also answers k-NN : `ORDER BY col <-> target LIMIT k` is satisfied directly from the index, with no full sort. Source : postgresql.org/docs/17/indexes-types.html, postgresql.org/docs/17/rangetypes.html.

### Pattern 6 : BRIN for very large naturally-ordered tables

ALWAYS use BRIN when the table is huge AND the indexed column correlates with physical row order (append-only event logs, time-series).
NEVER use BRIN on a column whose values are scattered across the heap , it degrades to a full scan while still costing maintenance.

```sql
-- 500M-row event log, rows inserted in created_at order
CREATE INDEX idx_events_created_brin
    ON events USING BRIN (created_at) WITH (pages_per_range = 128);
```

WHY : BRIN stores only the min/max of the indexed column per block range (default 128 pages). A range query reads the tiny BRIN summary, discards block ranges that cannot match, and scans only the survivors. The index is orders of magnitude smaller than a B-tree , but the entire premise is physical-order correlation. If rows for one date are spread across the whole table, every block range matches and BRIN buys nothing. Source : postgresql.org/docs/17/brin.html.

### Pattern 7 : INCLUDE covering index for an index-only scan

ALWAYS add frequently-read non-filter columns via `INCLUDE` so the planner can use an index-only scan.
NEVER widen the index **key** with payload columns , key columns cost ordering and uniqueness comparisons; `INCLUDE` columns do not.

```sql
-- Query : SELECT director, rating FROM films WHERE title = $1
CREATE UNIQUE INDEX idx_films_title
    ON films (title) INCLUDE (director, rating);
```

WHY : an index-only scan answers a query from the index alone, skipping every heap fetch , but only when the index carries all referenced columns AND the visibility map marks the pages all-visible (kept current by VACUUM). `INCLUDE` columns (v11+, supported by B-tree, GiST, SP-GiST) ride along as payload : they do not participate in uniqueness or sort order and need no operator class. Caveats : a wider index, B-tree deduplication is disabled when `INCLUDE` is present, and the tuple can hit the max index-tuple-size limit. Source : postgresql.org/docs/17/sql-createindex.html, postgresql.org/docs/17/indexes-index-only-scans.html.

### Pattern 8 : CREATE INDEX CONCURRENTLY and INVALID recovery

ALWAYS use `CREATE INDEX CONCURRENTLY` on a live table , a plain `CREATE INDEX` holds `ACCESS EXCLUSIVE` and blocks all writes for the whole build.
NEVER leave a failed concurrent build in place , a failed `CONCURRENTLY` run leaves an `INVALID` index that is ignored by queries but still slows down every write.

```sql
-- Online build : SHARE UPDATE EXCLUSIVE, writes continue
CREATE INDEX CONCURRENTLY idx_orders_customer ON orders (customer_id);

-- After a failure : find and drop the INVALID index, then retry
SELECT i.indexrelid::regclass
FROM pg_index i WHERE NOT i.indisvalid;

DROP INDEX CONCURRENTLY idx_orders_customer;   -- then re-run the CREATE
-- or, to rebuild in place : REINDEX INDEX CONCURRENTLY idx_orders_customer;  (v12+)
```

WHY : `CONCURRENTLY` does two table scans and waits out concurrent transactions, so it is slower in wall-clock time but never blocks writers. It CANNOT run inside a transaction block (SQLSTATE `25001` if attempted) and is not supported directly on a partitioned table , build per-partition then `ALTER INDEX ... ATTACH PARTITION`. `REINDEX ... CONCURRENTLY` (v12+) rebuilds a bloated index online. Source : postgresql.org/docs/17/sql-createindex.html, postgresql.org/docs/17/sql-reindex.html.

### Pattern 9 : Index every foreign-key column

ALWAYS create an index on the child-side column of every foreign key.
NEVER rely on the parent's primary-key index to cover the child , PostgreSQL does NOT auto-create the child index.

```sql
-- order_items.order_id REFERENCES orders(id) , the FK does NOT create this index
CREATE INDEX idx_order_items_order_id ON order_items (order_id);
```

WHY : the parent side of a foreign key is unique by definition, but the child column is not indexed automatically. Without the child index, every `DELETE` or key-`UPDATE` on the parent triggers a Seq Scan of the entire child table to check for referencing rows , and `ON DELETE CASCADE` does this for each cascaded row. Joins on the FK also fall back to Seq Scan / Hash Join on large data. Source : postgresql.org/docs/17/ddl-constraints.html.

## Anti-Patterns :

(One-liners ; full diagnosis + fix in `references/anti-patterns.md`. Most indexing faults are silent performance failures with no SQLSTATE.)

- Missing index on a foreign-key column , parent DELETE/UPDATE Seq-scans the child table; cascade deletes are O(n) per row.
- Expression-index mismatch , index on `(lower(email))`, query says `WHERE email = $1`; index never used.
- Redundant index , a single-column index on `(a)` is already covered by the leftmost prefix of `(a, b, c)`; drop the narrow one.
- Plain B-tree on a low-cardinality boolean , indexes every row; use a partial index `WHERE flag` instead.
- Too many indexes , every index is maintained on every INSERT/UPDATE; surplus indexes slow writes and bloat the table.
- Wrong access method , plain B-tree on a `jsonb` column expecting `@>` to be indexed; `@>` needs GIN.
- Range column first in a multicolumn index , `(created_at, tenant_id)` cannot serve `WHERE tenant_id = $1`.
- `CREATE INDEX` (non-concurrent) on a live table , `ACCESS EXCLUSIVE` blocks every writer for the whole build.
- Leftover `INVALID` index from a failed `CONCURRENTLY` build , ignored by reads, still slows writes : SQLSTATE `25001` if run in a transaction block.
- Non-`IMMUTABLE` function in an index expression , rejected with SQLSTATE `42P17`.
- BRIN on an uncorrelated column , every block range matches; degrades to a full scan.
- Implicit cast defeats the index , `WHERE int_col = '123'` or `WHERE varchar_col = 123` casts per row; cast the parameter to match the column type.
- No `ANALYZE` after creating a partial or expression index , planner has no statistics on the computed values and avoids the index.

## Reference Links :

- [references/methods.md](references/methods.md) : Full access-method capability matrix, operator classes, storage parameters, `CREATE INDEX` syntax, modifier rules
- [references/examples.md](references/examples.md) : Verified, version-annotated examples for every index method and modifier, plus audit queries for missing / redundant / unused indexes
- [references/anti-patterns.md](references/anti-patterns.md) : Indexing anti-patterns with cause, symptom, SQLSTATE (where applicable), and fix pattern

## See Also :

- `postgres-impl-explain-analyze` : reading `EXPLAIN` output to confirm an index is actually used (Index Scan vs Seq Scan vs Bitmap)
- `postgres-syntax-jsonb` : JSONB operators and the `jsonb_ops` vs `jsonb_path_ops` GIN tradeoff
- `postgres-impl-vacuum-bloat` : index bloat detection and `REINDEX CONCURRENTLY`; the visibility map that enables index-only scans
- `postgres-syntax-dml-ddl` : range types and exclusion constraints backed by GiST
- `postgres-core-version-matrix` : v11 added `INCLUDE`, v12 added `REINDEX CONCURRENTLY`, v10 made Hash indexes WAL-logged
- Vooronderzoek section : §4 (Indexing Strategy)
- Official docs : https://www.postgresql.org/docs/17/indexes-types.html, https://www.postgresql.org/docs/17/indexes-partial.html, https://www.postgresql.org/docs/17/sql-createindex.html, https://www.postgresql.org/docs/17/indexes-expressional.html
