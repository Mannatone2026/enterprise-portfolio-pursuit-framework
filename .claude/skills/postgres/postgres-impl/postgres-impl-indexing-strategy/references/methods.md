# postgres-impl-indexing-strategy : Methods Reference

API surface for PostgreSQL indexing. Verified against postgresql.org/docs/17 (Last Verified : 2026-05-19). Applies to PostgreSQL 15, 16, 17.

## CREATE INDEX syntax

```sql
CREATE [ UNIQUE ] INDEX [ CONCURRENTLY ] [ [ IF NOT EXISTS ] name ]
    ON [ ONLY ] table_name [ USING method ]
    ( { column_name | ( expression ) } [ COLLATE collation ]
      [ opclass [ ( opclass_parameter = value [, ...] ) ] ]
      [ ASC | DESC ] [ NULLS { FIRST | LAST } ] [, ...] )
    [ INCLUDE ( column_name [, ...] ) ]
    [ NULLS [ NOT ] DISTINCT ]
    [ WITH ( storage_parameter [= value] [, ...] ) ]
    [ TABLESPACE tablespace_name ]
    [ WHERE predicate ]
```

- `USING method` : `btree` (default), `hash`, `gist`, `spgist`, `gin`, `brin`.
- `( expression )` : double parentheses required for an expression index , `((lower(title)))`.
- `INCLUDE` : non-key payload columns (v11+).
- `NULLS NOT DISTINCT` (v15+) : a `UNIQUE` index treats NULLs as equal, so at most one NULL is allowed.
- `WHERE predicate` : partial index. May reference any table column, not only indexed ones. No subqueries, no aggregates, immutable functions only.

## Access-method capability matrix

| Capability | B-tree | Hash | GiST | SP-GiST | GIN | BRIN |
|---|---|---|---|---|---|---|
| Equality `=` | yes | yes | via opclass | via opclass | via opclass | yes |
| Range `< <= >= >` | yes | no | via opclass | via opclass | via opclass | yes |
| `BETWEEN` / `IN` | yes | no | no | no | no | yes |
| `IS NULL` / `IS NOT NULL` | yes | no | no | no | no | no |
| `LIKE 'prefix%'` / `~ '^anchor'` | yes | no | no | no | no | no |
| Sorted output (`ORDER BY`) | yes | no | no | no | no | no |
| k-NN (`ORDER BY col <-> x`) | no | no | yes | yes | no | no |
| `UNIQUE` / primary key | yes | no | no | no | no | no |
| Exclusion constraint | no | no | yes | yes | no | no |
| Multicolumn index | yes | no | yes | no | yes | yes |
| `INCLUDE` columns (v11+) | yes | no | yes | yes | no | no |
| `fillfactor` storage param | yes | yes | yes | yes | no | no |

Multicolumn support : ONLY B-tree, GiST, GIN, BRIN. Hash and SP-GiST are single-column only (postgresql.org/docs/17/indexes-multicolumn.html).

## Access-method selection summary

| Method | Use for | Key tradeoff |
|---|---|---|
| B-tree | equality, range, sort, `UNIQUE`, `LIKE 'pre%'` | the default ; only method for unique + sort |
| Hash | pure equality, measured win only | no sort, no unique, no multicolumn ; WAL-logged since v10 |
| GiST | ranges, geometry/PostGIS, exclusion, k-NN, FTS | lossy ; updatable ; opclass-dependent operators |
| SP-GiST | non-balanced data : IP-prefix, quadtree points, text-prefix | single-column only |
| GIN | `jsonb`, arrays, `tsvector`, `pg_trgm` | fast lookup, slow update (`fastupdate` pending list) |
| BRIN | huge tables physically ordered by the column | tiny ; useless without physical-order correlation |

## Common operator classes

| Column type | Method | Operator class | Notes |
|---|---|---|---|
| `text` case-insensitive prefix | B-tree | `text_pattern_ops` | needed for `LIKE 'x%'` under non-C collation |
| `varchar` prefix | B-tree | `varchar_pattern_ops` | same purpose for `varchar` |
| `jsonb` | GIN | `jsonb_ops` (default) | supports `@>`, `?`, `?|`, `?&`, `@?`, `@@` |
| `jsonb` | GIN | `jsonb_path_ops` | `@>`, `@?`, `@@` only ; smaller + faster |
| `text` substring | GIN / GiST | `gin_trgm_ops` / `gist_trgm_ops` | requires `pg_trgm` ; powers `LIKE '%mid%'` |
| `tsvector` | GIN / GiST | `tsvector_ops` | GIN faster lookup, GiST faster update |
| range type | GiST | range opclass | overlap `&&`, exclusion constraints |
| `inet` / `cidr` | SP-GiST | `inet_ops` | prefix-hierarchy search |
| `timestamptz` ordered table | BRIN | `timestamptz_minmax_ops` | block-range min/max |

Rule : the index operator class MUST match the operator the query uses, otherwise the index is not consulted.

## Index modifiers

### Partial index

`CREATE INDEX ... WHERE predicate` , indexes only rows matching `predicate`.

- The planner uses a partial index ONLY when the query `WHERE` clause **mathematically implies** the index predicate.
- A parameterised predicate (`WHERE x < $1`) cannot match a literal index predicate (`WHERE x < 100`) , the planner cannot prove the implication at plan time.
- Run `ANALYZE` after creation , the planner needs statistics on the subset.

### Expression index

`CREATE INDEX ... ((expression))` , indexes the result of `expression`.

- The query must contain the **identical** expression to match.
- All functions and operators in the expression must be `IMMUTABLE`. A non-immutable function is rejected with SQLSTATE `42P17` (`invalid_object_definition`).
- Run `ANALYZE` after creation , statistics are gathered on the computed values.

### Multicolumn index

`CREATE INDEX ... (a, b, c)` , a composite key.

- Serves leftmost-prefix predicates only : `(a)`, `(a, b)`, `(a, b, c)`. Does NOT serve `(b)` or `(b, c)` alone.
- Column-ordering rule : equality-predicate columns first (most selective leftmost), then ONE range / inequality / sort column last. A range predicate stops index probing on every column to its right.

### INCLUDE columns (v11+)

`CREATE INDEX ... (key_cols) INCLUDE (payload_cols)`.

- Payload columns ride along but are not part of the key : no ordering, no uniqueness, no operator class required.
- Enables an index-only scan when the query reads only key + payload columns.
- Supported by B-tree, GiST, SP-GiST. NOT by Hash, GIN, BRIN.
- Disables B-tree deduplication. A wide `INCLUDE` set can exceed the maximum index-tuple size.

## CREATE INDEX CONCURRENTLY

| Aspect | `CREATE INDEX` | `CREATE INDEX CONCURRENTLY` |
|---|---|---|
| Lock held | `ACCESS EXCLUSIVE` (blocks reads + writes) | `SHARE UPDATE EXCLUSIVE` (writes continue) |
| Table scans | one | two |
| Wall-clock speed | faster | slower |
| Inside a transaction block | allowed | NOT allowed , SQLSTATE `25001` |
| On a partitioned table | allowed (recurses to partitions) | not directly , build per partition, then `ALTER INDEX ... ATTACH PARTITION` |
| On failure | nothing left behind (rolled back) | leaves an `INVALID` index |

Detect an invalid index :

```sql
SELECT indexrelid::regclass AS index, indrelid::regclass AS table
FROM pg_index WHERE NOT indisvalid;
```

Recovery : `DROP INDEX CONCURRENTLY <name>` then re-run, or `REINDEX INDEX CONCURRENTLY <name>` (v12+).

## Storage parameters

| Parameter | Methods | Default | Purpose |
|---|---|---|---|
| `fillfactor` | B-tree, Hash, GiST, SP-GiST | 90 (B-tree) | leave free space per page for in-place updates |
| `deduplicate_items` | B-tree | on | merge duplicate keys ; auto-off when `INCLUDE` present |
| `fastupdate` | GIN | on | batch new entries in a pending list |
| `gin_pending_list_limit` | GIN | 4MB | pending-list flush threshold |
| `buffering` | GiST | auto | buffered build for large indexes |
| `pages_per_range` | BRIN | 128 | heap pages summarised per block range |
| `autosummarize` | BRIN | off | summarise new ranges automatically |

Note : `fillfactor` on a heap **table** (`CREATE TABLE ... WITH (fillfactor = 80)`) is a separate setting , it reserves page space so HOT (Heap-Only Tuple) updates can avoid index writes. Table `fillfactor` default is 100.

## Index-only scans and the visibility map

An index-only scan answers a query from the index alone, with no heap fetch. Two conditions must both hold :

1. The index carries every column the query references (key columns + `INCLUDE` columns).
2. The heap page is marked all-visible in the visibility map (maintained by VACUUM).

If VACUUM has not run recently, the visibility map is stale and the scan degrades to heap fetches. `EXPLAIN` reports `Heap Fetches: N` on an Index Only Scan node.

## Source URLs

- https://www.postgresql.org/docs/17/sql-createindex.html
- https://www.postgresql.org/docs/17/indexes-types.html
- https://www.postgresql.org/docs/17/indexes-multicolumn.html
- https://www.postgresql.org/docs/17/indexes-partial.html
- https://www.postgresql.org/docs/17/indexes-expressional.html
- https://www.postgresql.org/docs/17/indexes-index-only-scans.html
- https://www.postgresql.org/docs/17/sql-reindex.html
- https://www.postgresql.org/docs/17/gin.html
- https://www.postgresql.org/docs/17/brin.html
