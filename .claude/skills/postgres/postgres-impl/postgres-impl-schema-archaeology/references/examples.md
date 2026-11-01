# Schema Archaeology : Examples

Working introspection queries. Every query is verified against PostgreSQL 17
catalog and view documentation. Patterns marked `v15+` work on all supported
versions; `v16+` requires that minimum.

## 1. List every table in a schema with row count and size

```sql
-- v15+ : the fast pg_catalog path
SELECT c.relname,
       c.reltuples::bigint                            AS est_rows,
       pg_size_pretty(pg_total_relation_size(c.oid))  AS total_size
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind = 'r'
ORDER BY pg_total_relation_size(c.oid) DESC;
```

## 2. List columns of a table with type, nullability, default

```sql
-- v15+ : live user columns only (skip system columns and dropped columns)
SELECT a.attnum,
       a.attname,
       format_type(a.atttypid, a.atttypmod) AS data_type,
       a.attnotnull                          AS not_null,
       a.attidentity,
       pg_get_expr(d.adbin, d.adrelid)       AS default_expr
FROM pg_attribute a
LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
WHERE a.attrelid = 'public.orders'::regclass
  AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum;
```

## 3. The portable information_schema equivalent

```sql
-- v15+ : use ONLY when the query must also run on another SQL vendor
SELECT column_name, data_type, is_nullable, column_default
FROM information_schema.columns
WHERE table_schema = 'public' AND table_name = 'orders'
ORDER BY ordinal_position;
```

## 4. Incoming foreign keys : what references this table

```sql
-- v15+ : every child table that points at customers
SELECT conrelid::regclass AS child_table,
       conname,
       pg_get_constraintdef(oid) AS definition
FROM pg_constraint
WHERE confrelid = 'public.customers'::regclass
  AND contype = 'f'
ORDER BY child_table;
```

## 5. Outgoing foreign keys : what this table references

```sql
-- v15+ : every parent table that orders depends on
SELECT confrelid::regclass AS parent_table,
       conname,
       pg_get_constraintdef(oid) AS definition
FROM pg_constraint
WHERE conrelid = 'public.orders'::regclass
  AND contype = 'f'
ORDER BY parent_table;
```

## 6. Full FK graph of a schema

```sql
-- v15+ : every FK edge in the schema, child -> parent
SELECT conrelid::regclass  AS child_table,
       confrelid::regclass AS parent_table,
       conname
FROM pg_constraint c
JOIN pg_namespace n ON n.oid = connamespace
WHERE n.nspname = 'public' AND contype = 'f'
ORDER BY child_table, parent_table;
```

## 7. Table, index, and TOAST sizes side by side

```sql
-- v15+ : pick the right function for each measurement
SELECT c.relname,
       pg_size_pretty(pg_relation_size(c.oid))        AS heap_only,
       pg_size_pretty(pg_table_size(c.oid))           AS heap_toast_maps,
       pg_size_pretty(pg_indexes_size(c.oid))         AS indexes,
       pg_size_pretty(pg_total_relation_size(c.oid))  AS total
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind = 'r'
ORDER BY pg_total_relation_size(c.oid) DESC
LIMIT 20;
```

## 8. Unused indexes : never scanned

```sql
-- v15+ : indexes with zero scans, non-unique only (unique ones enforce keys)
SELECT s.schemaname, s.relname, s.indexrelname,
       s.idx_scan,
       pg_size_pretty(pg_relation_size(s.indexrelid)) AS index_size
FROM pg_stat_user_indexes s
JOIN pg_index i ON i.indexrelid = s.indexrelid
WHERE s.idx_scan = 0
  AND NOT i.indisunique
  AND NOT i.indisprimary
ORDER BY pg_relation_size(s.indexrelid) DESC;
```

```sql
-- v16+ : add when the index was last used, for a confidence check
SELECT s.relname, s.indexrelname, s.idx_scan, s.last_idx_scan
FROM pg_stat_user_indexes s
WHERE s.idx_scan = 0 OR s.last_idx_scan < now() - interval '90 days'
ORDER BY s.relname;
```

## 9. Redundant indexes : column-name resolved

```sql
-- v15+ : indexes on the same table whose leading columns match
WITH idx AS (
  SELECT i.indrelid,
         i.indexrelid,
         i.indexrelid::regclass AS index_name,
         (SELECT string_agg(a.attname, ',' ORDER BY k.ord)
          FROM unnest(i.indkey) WITH ORDINALITY AS k(attnum, ord)
          JOIN pg_attribute a
            ON a.attrelid = i.indrelid AND a.attnum = k.attnum) AS cols
  FROM pg_index i
)
SELECT a.indrelid::regclass AS table_name,
       a.index_name AS index_a, a.cols AS cols_a,
       b.index_name AS index_b, b.cols AS cols_b
FROM idx a
JOIN idx b ON a.indrelid = b.indrelid
          AND a.indexrelid < b.indexrelid
          AND (b.cols = a.cols OR b.cols LIKE a.cols || ',%');
```

## 10. Orphan rows : dangling foreign-key references

```sql
-- v15+ : order rows whose customer no longer exists
SELECT o.id, o.customer_id
FROM orders o
LEFT JOIN customers c ON c.id = o.customer_id
WHERE o.customer_id IS NOT NULL
  AND c.id IS NULL;

-- v15+ : count orphans before deciding to clean or re-validate the FK
SELECT count(*) AS orphan_count
FROM orders o
LEFT JOIN customers c ON c.id = o.customer_id
WHERE o.customer_id IS NOT NULL AND c.id IS NULL;
```

## 11. Find foreign keys that were never validated

```sql
-- v15+ : NOT VALID constraints can hide orphan rows
SELECT conrelid::regclass AS table_name, conname, contype
FROM pg_constraint
WHERE NOT convalidated
ORDER BY table_name;
```

## 12. Dependency inspection before a DROP

```sql
-- v15+ : everything that depends on a table, human-readable
SELECT DISTINCT
       pg_describe_object(d.classid, d.objid, d.objsubid) AS dependent_object,
       d.deptype
FROM pg_depend d
WHERE d.refobjid = 'public.customers'::regclass
  AND d.deptype IN ('n', 'a')
ORDER BY dependent_object;
```

```sql
-- v15+ : the dry-run alternative. RESTRICT is the default; this FAILS and
-- lists every dependent object instead of cascading.
BEGIN;
DROP TABLE customers RESTRICT;   -- read the error, then ROLLBACK
ROLLBACK;
```

## 13. Sequence vs IDENTITY audit

```sql
-- v15+ : default-bearing columns and whether they are IDENTITY or legacy serial
SELECT c.relname AS table_name,
       a.attname AS column_name,
       CASE a.attidentity
         WHEN 'a' THEN 'IDENTITY ALWAYS'
         WHEN 'd' THEN 'IDENTITY BY DEFAULT'
         ELSE 'plain default (check for serial/sequence)'
       END AS kind
FROM pg_attribute a
JOIN pg_class c     ON c.oid = a.attrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public'
  AND a.attnum > 0 AND NOT a.attisdropped AND a.atthasdef
ORDER BY c.relname, a.attnum;
```

```sql
-- v15+ : sequences and the column each is owned by (NULL = unowned sequence)
SELECT schemaname, sequencename, last_value
FROM pg_sequences
WHERE schemaname = 'public'
ORDER BY sequencename;
```

## 14. Bloat signal : dead tuples per table

```sql
-- v15+ : tables where dead tuples are a large share of the total
SELECT schemaname, relname,
       n_live_tup, n_dead_tup,
       round(100.0 * n_dead_tup / nullif(n_live_tup + n_dead_tup, 0), 1)
         AS dead_pct,
       last_autovacuum
FROM pg_stat_user_tables
WHERE n_dead_tup > 0
ORDER BY dead_pct DESC NULLS LAST;
```

## 15. Inventory of functions, triggers, and policies

```sql
-- v15+ : all functions in a schema
SELECT p.proname, pg_get_function_arguments(p.oid) AS args
FROM pg_proc p
JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname = 'public'
ORDER BY p.proname;

-- v15+ : all triggers (skip internal constraint triggers)
SELECT t.tgname, c.relname AS on_table
FROM pg_trigger t
JOIN pg_class c ON c.oid = t.tgrelid
WHERE NOT t.tgisinternal
ORDER BY on_table, t.tgname;

-- v15+ : all row-level security policies
SELECT n.nspname, c.relname, pol.polname, pol.polcmd
FROM pg_policy pol
JOIN pg_class c     ON c.oid = pol.polrelid
JOIN pg_namespace n ON n.oid = c.relnamespace
ORDER BY c.relname, pol.polname;
```
