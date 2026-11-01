# postgres-impl-indexing-strategy : Examples

Verified, version-annotated index examples. All SQL verified against postgresql.org/docs/17 (Last Verified : 2026-05-19). Patterns work on PostgreSQL 15, 16, 17 unless a version tag says otherwise.

## B-tree examples

### Single-column equality + range

```sql
-- v15+ : the default method, no USING clause needed
CREATE INDEX idx_orders_created ON orders (created_at);

-- serves : WHERE created_at = $1
--          WHERE created_at BETWEEN $1 AND $2
--          WHERE created_at >= $1 ORDER BY created_at
```

### Multicolumn, equality columns first

```sql
-- v15+ : query is WHERE tenant_id = $1 AND status = $2 AND created_at >= $3
CREATE INDEX idx_orders_tenant_status_created
    ON orders (tenant_id, status, created_at);

-- leftmost-prefix usable :
--   WHERE tenant_id = $1
--   WHERE tenant_id = $1 AND status = $2
--   WHERE tenant_id = $1 AND status = $2 AND created_at >= $3
-- NOT usable : WHERE status = $2  (no leading tenant_id)
```

### Prefix LIKE under a non-C collation

```sql
-- v15+ : text_pattern_ops is required for LIKE 'prefix%' when the
-- database collation is not C / POSIX
CREATE INDEX idx_users_name_prefix
    ON users (last_name text_pattern_ops);

SELECT * FROM users WHERE last_name LIKE 'Sch%';
```

### Unique index with NULLS NOT DISTINCT (v15+)

```sql
-- v15+ : at most one NULL allowed (default UNIQUE allows many NULLs)
CREATE UNIQUE INDEX idx_devices_serial
    ON devices (serial_number) NULLS NOT DISTINCT;
```

## Partial index examples

```sql
-- v15+ : soft-delete , every query carries WHERE deleted_at IS NULL
CREATE INDEX idx_users_active_email
    ON users (email) WHERE deleted_at IS NULL;

-- v15+ : low-cardinality flag , index only the rare unprocessed rows
CREATE INDEX idx_jobs_pending
    ON jobs (created_at) WHERE NOT processed;

-- v15+ : partial UNIQUE , only one active subscription per user
CREATE UNIQUE INDEX idx_one_active_sub
    ON subscriptions (user_id) WHERE status = 'active';

ANALYZE users;   -- always ANALYZE after creating a partial index
```

The planner uses `idx_users_active_email` for `WHERE deleted_at IS NULL AND email = $1`. It does NOT use it for `WHERE email = $1` alone, because that query does not imply `deleted_at IS NULL`.

## Expression index examples

```sql
-- v15+ : case-insensitive lookup
CREATE INDEX idx_users_lower_email ON users ((lower(email)));
SELECT * FROM users WHERE lower(email) = lower($1);   -- uses the index

-- v15+ : index a value extracted from JSONB
CREATE INDEX idx_events_user ON events (((payload ->> 'user_id')));
SELECT * FROM events WHERE (payload ->> 'user_id') = $1;

-- v15+ : index a date truncation
CREATE INDEX idx_logs_day ON logs ((date_trunc('day', logged_at)));
SELECT * FROM logs WHERE date_trunc('day', logged_at) = DATE '2026-05-20';

ANALYZE events;   -- always ANALYZE after creating an expression index
```

The query expression must match the index expression exactly. `WHERE email = $1` will NOT use `idx_users_lower_email`.

## GIN examples (jsonb, arrays, full-text)

```sql
-- v15+ : jsonb containment, default jsonb_ops (supports @>, ?, ?|, ?&)
CREATE INDEX idx_docs_data ON docs USING GIN (data);
SELECT * FROM docs WHERE data @> '{"status":"open"}';
SELECT * FROM docs WHERE data ? 'archived_at';

-- v15+ : jsonb_path_ops , smaller + faster, but @> / @? / @@ only
CREATE INDEX idx_docs_data_path ON docs USING GIN (data jsonb_path_ops);

-- v15+ : array overlap and containment
CREATE INDEX idx_posts_tags ON posts USING GIN (tags);
SELECT * FROM posts WHERE tags @> ARRAY['postgres'];
SELECT * FROM posts WHERE tags && ARRAY['sql','db'];

-- v15+ : full-text search
CREATE INDEX idx_articles_fts ON articles USING GIN (to_tsvector('english', body));
SELECT * FROM articles
WHERE to_tsvector('english', body) @@ plainto_tsquery('english', $1);

-- v15+ : substring search , requires the pg_trgm extension
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX idx_users_name_trgm ON users USING GIN (full_name gin_trgm_ops);
SELECT * FROM users WHERE full_name ILIKE '%anders%';
```

## GiST examples (ranges, exclusion, k-NN)

```sql
-- v15+ : exclusion constraint , no overlapping reservations of one room
CREATE TABLE reservation (
    room    text,
    during  tsrange,
    EXCLUDE USING GIST (room WITH =, during WITH &&)
);

-- v15+ : range-overlap query backed by GiST
CREATE INDEX idx_promo_period ON promotions USING GIST (active_period);
SELECT * FROM promotions WHERE active_period && tsrange($1, $2);

-- v15+ : k-nearest-neighbour ordering , GiST answers ORDER BY <-> directly
CREATE INDEX idx_places_geom ON places USING GIST (geom);
SELECT id FROM places ORDER BY geom <-> ST_Point(5.1, 52.1) LIMIT 10;
```

## SP-GiST example (non-balanced data)

```sql
-- v15+ : IP-prefix hierarchy , inet_ops is an SP-GiST operator class
CREATE INDEX idx_access_ip ON access_log USING SPGIST (client_ip inet_ops);
SELECT * FROM access_log WHERE client_ip << inet '10.0.0.0/8';
```

## BRIN example (huge naturally-ordered table)

```sql
-- v15+ : 500M-row append-only event log, physically ordered by created_at
CREATE INDEX idx_events_created_brin
    ON events USING BRIN (created_at) WITH (pages_per_range = 128);

SELECT * FROM events
WHERE created_at >= DATE '2026-05-01' AND created_at < DATE '2026-05-02';
```

BRIN only helps because rows are inserted in `created_at` order. On a column with scattered values it degrades to a full scan.

## INCLUDE covering index (v11+)

```sql
-- v11+ : query is SELECT director, rating FROM films WHERE title = $1
CREATE UNIQUE INDEX idx_films_title
    ON films (title) INCLUDE (director, rating);

-- EXPLAIN shows : Index Only Scan using idx_films_title
EXPLAIN (ANALYZE, BUFFERS)
SELECT director, rating FROM films WHERE title = $1;
```

## CREATE INDEX CONCURRENTLY (online build)

```sql
-- v15+ : online build , runs OUTSIDE any transaction block
CREATE INDEX CONCURRENTLY idx_orders_customer ON orders (customer_id);

-- after a failed concurrent build : locate and drop the INVALID index
SELECT indexrelid::regclass AS index, indrelid::regclass AS table
FROM pg_index WHERE NOT indisvalid;

DROP INDEX CONCURRENTLY idx_orders_customer;   -- then re-run the CREATE

-- v12+ : rebuild a bloated index online
REINDEX INDEX CONCURRENTLY idx_orders_customer;
```

Partitioned table : `CREATE INDEX CONCURRENTLY` is not supported on the parent. Build per partition, then attach.

```sql
-- v15+ : per-partition online build, then attach to the parent index
CREATE INDEX idx_parent_created ON ONLY measurements (created_at);  -- invalid until all attached
CREATE INDEX CONCURRENTLY idx_m_2026_05 ON measurements_2026_05 (created_at);
ALTER INDEX idx_parent_created ATTACH PARTITION idx_m_2026_05;
```

## Index foreign-key columns

```sql
-- v15+ : the FK does NOT auto-create this index , create it explicitly
CREATE INDEX idx_order_items_order_id ON order_items (order_id);
```

## Audit queries

### Missing FK indexes

```sql
-- v15+ : FK columns on the child side that have no covering index
SELECT c.conrelid::regclass AS child_table,
       a.attname            AS fk_column
FROM pg_constraint c
JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey)
WHERE c.contype = 'f'
  AND NOT EXISTS (
      SELECT 1 FROM pg_index i
      WHERE i.indrelid = c.conrelid
        AND a.attnum = i.indkey[0]
  )
ORDER BY child_table;
```

### Unused indexes

```sql
-- v15+ : indexes never used since the last stats reset (drop candidates)
SELECT s.relname        AS table,
       s.indexrelname   AS index,
       s.idx_scan       AS scans,
       pg_size_pretty(pg_relation_size(s.indexrelid)) AS size
FROM pg_stat_user_indexes s
JOIN pg_index i ON i.indexrelid = s.indexrelid
WHERE s.idx_scan = 0
  AND NOT i.indisunique          -- keep unique indexes, they enforce constraints
ORDER BY pg_relation_size(s.indexrelid) DESC;
```

### Invalid indexes

```sql
-- v15+ : indexes left INVALID by a failed CONCURRENTLY build
SELECT indexrelid::regclass AS index, indrelid::regclass AS table
FROM pg_index WHERE NOT indisvalid;
```

### Duplicate / redundant indexes

```sql
-- v15+ : indexes whose column list is identical (exact duplicates)
SELECT indrelid::regclass AS table,
       array_agg(indexrelid::regclass) AS duplicate_indexes
FROM pg_index
GROUP BY indrelid, indkey, indclass, indexprs, indpred
HAVING count(*) > 1;
```
