---
name: postgres-impl-schema-archaeology
description: >
  Use when exploring an unfamiliar database, finding what references a table, locating unused or redundant indexes, or auditing a mature schema.
  Prevents slow introspection from using information_schema where pg_catalog is faster, DROP cascade surprises from skipping pg_depend, and missing orphan rows or wraparound risk in a legacy database.
  Covers information_schema vs pg_catalog, FK-graph navigation via pg_constraint, table + index size queries, unused index detection (pg_stat_user_indexes), redundant index detection, orphan row queries, sequence vs IDENTITY audit, pg_depend dependency graph, psql introspection shortcuts.
  Keywords: pg_catalog, information_schema, pg_class, pg_attribute, pg_constraint, pg_depend, pg_stat_user_indexes, pg_relation_size, foreign key graph, unused index, redundant index, orphan rows, schema introspection, how to explore a database, what references this table, find unused indexes, inherited a legacy database
license: MIT
compatibility: "Designed for Claude Code. Requires PostgreSQL 15, 16, or 17."
metadata:
  author: OpenAEC-Foundation
  version: "1.0"
---

# postgres-impl-schema-archaeology

## Quick Reference :

Schema archaeology is the disciplined inspection of an existing database : its
tables, columns, constraints, indexes, sizes, dependencies, and waste. Two
introspection surfaces exist :

```
pg_catalog          PostgreSQL-specific, complete, fast. The default tool.
information_schema  SQL-standard, portable across vendors, slower, incomplete.
```

ALWAYS use `pg_catalog` for inspection inside PostgreSQL. Use `information_schema`
ONLY when the same query must also run on another SQL database.

The fastest first pass is psql meta-commands :

| Command | Shows | `+` variant adds |
|---------|-------|------------------|
| `\d name` | one object : columns, indexes, constraints | `\d+` size, storage, description |
| `\dt` | tables | `\dt+` size, description |
| `\di` | indexes | `\di+` size |
| `\dv` | views | `\dv+` definition source |
| `\df` | functions | `\df+` source, volatility |
| `\dn` | schemas | `\dn+` access privileges |
| `\dp` | table access privileges | |
| `\du` | roles | `\du+` description |
| `\dx` | installed extensions | |
| `\det` | foreign tables | `\det+` server, options |
| `\l` | databases | `\l+` size, description |

For programmatic audits, query the catalogs directly. The core catalog objects :
`pg_class` (tables, indexes, views), `pg_attribute` (columns), `pg_constraint`
(keys, checks), `pg_depend` (dependency graph), `pg_stat_user_indexes` and
`pg_stat_user_tables` (usage statistics).

## When To Use This Skill :

ALWAYS use this skill when :
- Exploring a database you did not design and have no documentation for
- Finding every table that references a given table by foreign key
- Locating indexes that are never scanned, or duplicate/redundant indexes
- Measuring which tables and indexes consume the most disk
- Checking what would break before a `DROP TABLE` / `DROP COLUMN`
- Auditing a legacy schema for `serial` columns, orphan rows, or bloat

NEVER use this skill for :
- Designing a new schema from scratch (use postgres-syntax-ddl)
- Reading or fixing a slow query plan (use postgres-impl-explain-tuning)
- Removing dead-tuple bloat itself (use postgres-core-vacuum-bloat)

## Decision Trees :

### information_schema vs pg_catalog :

```
Does this query need to run on a non-PostgreSQL database too?
├── YES -> information_schema (SQL-standard, portable)
└── NO  -> pg_catalog
          ├── It is faster (no standard-mandated view joins)
          ├── It is complete (exposes indexes, stats, OIDs, system columns)
          └── It is the only place for PostgreSQL-specific metadata
```

### What breaks if I DROP this object :

```
About to DROP a table, column, type, or function?
├── 1. psql : run \d on the object, read the "Referenced by" section
├── 2. SQL  : query pg_depend for rows where refobjid = the object's OID
├── 3. Dry run : DROP ... RESTRICT (the default). It FAILS and lists every
│       dependent object instead of silently cascading.
└── 4. Only after reviewing the list : DROP ... CASCADE, deliberately.
       NEVER type CASCADE before seeing the RESTRICT failure list.
```

### Finding schema waste :

```
What kind of waste?
├── Index never used        -> pg_stat_user_indexes WHERE idx_scan = 0
├── Two indexes, same job   -> compare leading column lists (redundant index)
├── Dead-tuple bloat        -> pg_stat_user_tables : high n_dead_tup
├── Rows with no parent     -> LEFT JOIN parent, WHERE parent key IS NULL
└── Legacy serial columns   -> pg_attribute.attidentity = '' on a sequence-fed col
```

## Patterns :

### Pattern 1 : pg_catalog over information_schema for introspection

ALWAYS query `pg_catalog` for introspection inside PostgreSQL. NEVER use
`information_schema` for performance-sensitive or PostgreSQL-specific lookups.

```sql
-- List every table in a schema with its size, fast path :
SELECT c.relname,
       pg_size_pretty(pg_total_relation_size(c.oid)) AS total_size
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind = 'r'
ORDER BY pg_total_relation_size(c.oid) DESC;
```

WHY : `information_schema` views are defined by the SQL standard and built from
multi-way joins over the same catalogs, plus permission filtering. They are
slower and cannot expose indexes, OIDs, statistics, or system columns. Reach for
`information_schema` ONLY when the query must be portable to another vendor.

### Pattern 2 : FK-graph navigation via pg_constraint

ALWAYS query `pg_constraint` with `contype = 'f'` to map foreign-key edges.
NEVER assume a table has no children just because its own definition is clean.

```sql
-- What references this table (incoming FKs) :
SELECT conrelid::regclass AS child_table, conname
FROM pg_constraint
WHERE confrelid = 'public.customers'::regclass AND contype = 'f';

-- What this table references (outgoing FKs) :
SELECT confrelid::regclass AS parent_table, conname
FROM pg_constraint
WHERE conrelid = 'public.orders'::regclass AND contype = 'f';
```

WHY : `conrelid` is the table the constraint lives on (the child), `confrelid` is
the referenced table (the parent). `contype` codes : `f` foreign key, `p` primary
key, `u` unique, `c` check, `x` exclusion. Walking these edges reconstructs the
full referential graph of an undocumented database.

### Pattern 3 : Table and index size queries

ALWAYS pick the size function that matches what you are measuring. NEVER use
`pg_relation_size` when you mean the table plus its indexes and TOAST.

```sql
SELECT c.relname,
       pg_size_pretty(pg_table_size(c.oid))         AS heap_and_toast,
       pg_size_pretty(pg_indexes_size(c.oid))       AS all_indexes,
       pg_size_pretty(pg_total_relation_size(c.oid)) AS total
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind = 'r'
ORDER BY pg_total_relation_size(c.oid) DESC;
```

WHY : `pg_relation_size` is the main data fork only. `pg_table_size` is heap +
TOAST + FSM + VM, indexes excluded. `pg_indexes_size` is all indexes only.
`pg_total_relation_size` is everything, equal to `pg_table_size + pg_indexes_size`.
Wrap any of them in `pg_size_pretty` for human-readable output.

### Pattern 4 : Unused index detection

ALWAYS find never-scanned indexes via `pg_stat_user_indexes` with `idx_scan = 0`.
NEVER drop one without confirming the statistics window is long enough.

```sql
SELECT s.schemaname, s.relname, s.indexrelname,
       s.idx_scan, s.last_idx_scan,
       pg_size_pretty(pg_relation_size(s.indexrelid)) AS index_size
FROM pg_stat_user_indexes s
JOIN pg_index i ON i.indexrelid = s.indexrelid
WHERE s.idx_scan = 0
  AND NOT i.indisunique          -- keep unique indexes : they enforce constraints
ORDER BY pg_relation_size(s.indexrelid) DESC;
```

WHY : `idx_scan` counts how often the planner chose that index. Zero means it has
paid write and storage cost without ever serving a read. `last_idx_scan` (v16+)
shows when it was last used. ALWAYS exclude `indisunique` indexes : dropping them
removes a constraint. Confirm statistics were not reset recently before acting.

### Pattern 5 : Redundant index detection

ALWAYS treat an index as redundant when another index has the same leading
columns in the same order. NEVER keep both without a measured reason.

```sql
-- Indexes on the same table whose column lists overlap at the start :
SELECT n.nspname, c.relname AS table_name,
       i1.indexrelid::regclass AS index_a,
       i2.indexrelid::regclass AS index_b
FROM pg_index i1
JOIN pg_index i2 ON i1.indrelid = i2.indrelid
                AND i1.indexrelid < i2.indexrelid
JOIN pg_class c ON c.oid = i1.indrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE i1.indkey::text = i2.indkey::text          -- identical column list
   OR i2.indkey::text LIKE i1.indkey::text || ' %'; -- i1 is a prefix of i2
```

WHY : a B-tree index on `(a)` is fully served by an index on `(a, b)`, because a
multi-column B-tree supports any leading-column prefix. Keeping the narrower
index doubles write cost for no read benefit. See references/examples.md for the
column-name resolved version.

### Pattern 6 : Orphan row detection

ALWAYS find orphan rows with a `LEFT JOIN` to the parent and a NULL test on the
parent key. NEVER assume referential integrity holds on a table whose FK was
added `NOT VALID` and never validated.

```sql
-- Order rows whose customer_id points at a customer that does not exist :
SELECT o.id, o.customer_id
FROM orders o
LEFT JOIN customers c ON c.id = o.customer_id
WHERE o.customer_id IS NOT NULL
  AND c.id IS NULL;
```

WHY : a `LEFT JOIN` keeps every left row; an unmatched right side is all NULL, so
`c.id IS NULL` isolates children with a dangling reference. Orphans appear when an
FK was created `NOT VALID`, when data was loaded with constraints dropped, or when
`ON DELETE` rules were changed after the fact.

### Pattern 7 : Dependency check before DROP

ALWAYS inspect `pg_depend` (or run `DROP ... RESTRICT` first) before dropping any
object. NEVER reach for `CASCADE` until you have read the dependent-object list.

```sql
-- Everything that depends on a table, by OID :
SELECT DISTINCT pg_describe_object(d.classid, d.objid, d.objsubid) AS dependent
FROM pg_depend d
WHERE d.refobjid = 'public.customers'::regclass
  AND d.deptype IN ('n', 'a');
```

WHY : `DROP` defaults to `RESTRICT` and fails with SQLSTATE 2BP01
`dependent_objects_still_exist`, listing every blocker. `pg_depend.deptype` : `n`
normal (needs CASCADE), `a` auto (drops with its owner), `i` internal,
`e` extension member. `pg_describe_object` turns the raw OID triple into a
readable name. `CASCADE` typed blind silently destroys views, FKs, and functions.

### Pattern 8 : Sequence vs IDENTITY audit

ALWAYS audit `pg_attribute.attidentity` to find legacy `serial` columns. NEVER
leave a key column on `serial` when `IDENTITY` is the correct modern form.

```sql
-- Columns fed by a sequence : '' = serial/manual default, 'a'/'d' = IDENTITY
SELECT c.relname AS table_name, a.attname AS column_name,
       a.attidentity
FROM pg_attribute a
JOIN pg_class c ON c.oid = a.attrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND a.attnum > 0 AND NOT a.attisdropped
  AND a.atthasdef
ORDER BY c.relname, a.attnum;
```

WHY : `attidentity` is `a` for `GENERATED ALWAYS AS IDENTITY`, `d` for
`GENERATED BY DEFAULT AS IDENTITY`, and empty for everything else including
`serial`. A `serial` column owns an implicit sequence with its own privilege and
type-change foot-guns; `IDENTITY` (v10+) is the SQL-standard replacement. Cross-
check `pg_sequences` for sequences not owned by any column.

## Anti-Patterns :

(Full cause + symptom + fix in references/anti-patterns.md)

- `information_schema` for hot introspection : far slower than `pg_catalog`
- `DROP ... CASCADE` without checking `pg_depend` : SQLSTATE 2BP01 silenced
- Dropping a never-scanned index that is `indisunique` : removes a constraint
- Trusting physical column order : dropped columns leave gaps in `attnum`
- Ignoring system columns : `ctid`, `xmin`, `xmax` are not in `\d` output
- Reading `idx_scan = 0` right after `pg_stat_reset()` : false "unused" verdict

## Reference Links :

- [references/methods.md](references/methods.md) : Catalog and view column reference, size functions, psql meta-commands
- [references/examples.md](references/examples.md) : Working introspection queries, version-annotated
- [references/anti-patterns.md](references/anti-patterns.md) : Anti-patterns with cause, symptom, SQLSTATE, and fix

## See Also :

- postgres-impl-indexing-strategy : acting on unused/redundant index findings
- postgres-core-vacuum-bloat : interpreting `n_dead_tup` and fixing bloat
- postgres-impl-zero-downtime-migration : safely dropping audited objects online
- Vooronderzoek section : §2 (Full SQL Surface), §6 (Performance + EXPLAIN)
- Official docs : https://www.postgresql.org/docs/17/catalogs.html
- Official docs : https://www.postgresql.org/docs/17/monitoring-stats.html
- Official docs : https://www.postgresql.org/docs/17/information-schema.html
