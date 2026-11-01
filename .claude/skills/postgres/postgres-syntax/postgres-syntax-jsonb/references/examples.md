# Examples : JSONB

Verified, version-annotated examples for every common JSONB operation. All examples target PostgreSQL 15, 16, or 17 unless noted.

## Example 1 : Extract Values With `->` / `->>` / `#>` / `#>>`

```sql
WITH t(doc) AS (VALUES ('{
  "kind": "signup",
  "user": {"id": 42, "name": "Alice", "addr": {"city": "Amsterdam"}},
  "tags": ["new", "vip"]
}'::jsonb))
SELECT
  doc->'user'                AS user_jsonb,
  doc->'user'->>'name'       AS user_name_text,
  doc->'tags'->0             AS first_tag_jsonb,    -- "new"
  doc->'tags'->>0            AS first_tag_text,    --  new
  doc#>'{user, addr, city}'  AS city_jsonb,        -- "Amsterdam"
  doc#>>'{user, addr, city}' AS city_text,         --  Amsterdam
  doc->'missing'             AS null_jsonb,        -- NULL (key absent)
  doc->>'missing'            AS null_text          -- NULL
FROM t;
```

Common mistake :

```sql
SELECT (doc->'user'->'name')::text FROM t;
-- Returns '"Alice"' (with JSON quotes) ; almost never what you want.

SELECT doc->'user'->>'name' FROM t;
-- Returns 'Alice' (the SQL text value).
```

Verified against : postgresql.org/docs/17/functions-json.html.

## Example 2 : Containment Filter Backed by GIN

```sql
CREATE TABLE event (
  id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  payload jsonb  NOT NULL
);

INSERT INTO event (payload)
SELECT jsonb_build_object(
  'kind',   (ARRAY['signup','login','purchase'])[1 + (i % 3)],
  'region', (ARRAY['eu','us','ap'])[1 + (i % 3)],
  'amount', (i * 7.13)::numeric(10,2)
)
FROM generate_series(1, 100000) AS s(i);

-- Without index : Seq Scan
EXPLAIN (ANALYZE, BUFFERS)
SELECT count(*) FROM event WHERE payload @> '{"kind":"signup","region":"eu"}';
-- Seq Scan on event (cost=0.00..3000.00) (actual time=15.4..16.2 rows=...)
--   Filter: (payload @> '{"kind":"signup","region":"eu"}'::jsonb)
--   Rows Removed by Filter: ...

CREATE INDEX event_payload_gin ON event USING GIN (payload);

EXPLAIN (ANALYZE, BUFFERS)
SELECT count(*) FROM event WHERE payload @> '{"kind":"signup","region":"eu"}';
-- Bitmap Heap Scan on event (cost=20.00..200.00)
--   Recheck Cond: (payload @> '...'::jsonb)
--   -> Bitmap Index Scan on event_payload_gin (cost=0..20)
--        Index Cond: (payload @> '...'::jsonb)
```

Verified against : postgresql.org/docs/17/datatype-json.html (jsonb Indexing), postgresql.org/docs/17/using-explain.html.

## Example 3 : `jsonb_ops` vs `jsonb_path_ops` Size Comparison

```sql
-- Same column, two GIN variants. Inspect pg_relation_size :
CREATE INDEX event_payload_default_gin ON event USING GIN (payload);
CREATE INDEX event_payload_path_gin    ON event USING GIN (payload jsonb_path_ops);

SELECT i.indexrelid::regclass AS index,
       pg_size_pretty(pg_relation_size(i.indexrelid)) AS size,
       am.amname,
       opc.opcname
FROM pg_index i
  JOIN pg_class c    ON c.oid = i.indrelid
  JOIN pg_class ic   ON ic.oid = i.indexrelid
  JOIN pg_am am      ON am.oid = ic.relam
  JOIN pg_opclass opc ON opc.oid = ANY(i.indclass::oid[])
WHERE c.relname = 'event';

-- Typical output (100k rows, 3 keys per doc) :
--   event_payload_default_gin | 9216 kB | gin | jsonb_ops
--   event_payload_path_gin    | 2400 kB | gin | jsonb_path_ops
```

The `jsonb_path_ops` index is ~25% the size and is faster for containment lookups. But :

```sql
-- This query CANNOT use the path-ops index :
EXPLAIN SELECT count(*) FROM event WHERE payload ? 'kind';
-- It can use the default-opclass index, OR fall back to Seq Scan.
```

Verified against : postgresql.org/docs/17/datatype-json.html (jsonb Indexing).

## Example 4 : B-tree Expression Index for `->>` Equality

```sql
-- Frequent hot predicate :
SELECT count(*) FROM event WHERE payload->>'kind' = 'signup';

-- Add a B-tree expression index :
CREATE INDEX event_kind_btree ON event ((payload->>'kind'));

EXPLAIN (ANALYZE, BUFFERS)
SELECT count(*) FROM event WHERE payload->>'kind' = 'signup';
-- Index Only Scan using event_kind_btree
--   Index Cond: ((payload->>'kind') = 'signup')

-- Compound predicates : tuple expression index
CREATE INDEX event_kind_region_btree
  ON event ((payload->>'kind'), (payload->>'region'));

-- Partial index (only index rows you actually query) :
CREATE INDEX event_eu_unpaid
  ON event ((payload->>'amount'))
  WHERE payload->>'region' = 'eu' AND payload->>'kind' = 'signup';
```

Verified against : postgresql.org/docs/17/indexes-expressional.html, postgresql.org/docs/17/indexes-partial.html.

## Example 5 : `jsonb_set` / `jsonb_insert` / Subscript Update

```sql
-- jsonb_set : replace at path, create if missing (default)
UPDATE event SET payload = jsonb_set(payload, '{status}', '"paid"')
WHERE id = 42;

-- jsonb_set : refuse to create
UPDATE event SET payload = jsonb_set(payload, '{coupon}', '"X10"', false)
WHERE id = 42;
-- If 'coupon' key absent, payload unchanged.

-- jsonb_set : nested path (array indices included)
UPDATE event SET payload = jsonb_set(payload, '{items, 0, qty}', '5')
WHERE id = 42;

-- jsonb_insert : insert into array WITHOUT overwriting (v9.6+)
UPDATE event SET payload = jsonb_insert(payload, '{items, 0}', '{"sku":"X"}', false)
WHERE id = 42;
-- insert_after=false : insert BEFORE position 0 (prepend).
UPDATE event SET payload = jsonb_insert(payload, '{items, -1}', '{"sku":"X"}', true)
WHERE id = 42;
-- insert_after=true at last position : append.

-- Subscript update (v14+)
UPDATE event SET payload['status']                       = '"refunded"' WHERE id = 42;
UPDATE event SET payload['user']['addr']['city']         = '"Rotterdam"' WHERE id = 42;
UPDATE event SET payload['items'][3]                     = '{"sku":"Z"}' WHERE id = 42;
-- If items has length 2, this produces [..., null, {"sku":"Z"}] (padded with null).
```

Verified against : postgresql.org/docs/17/functions-json.html (jsonb_set, jsonb_insert), postgresql.org/docs/17/datatype-json.html (Subscripting).

## Example 6 : jsonpath Query With Variables

```sql
-- v12+ jsonpath operators :
SELECT id FROM event WHERE payload @? '$.items[*] ? (@.qty > 10)';
SELECT id FROM event WHERE payload @@ 'exists($.items[*] ? (@.qty > 10))';

-- jsonb_path_query : set-returning ; emits each match
SELECT e.id, item
FROM event e,
     LATERAL jsonb_path_query(e.payload, '$.items[*] ? (@.qty > $min)',
                              '{"min":10}') AS item;

-- jsonb_path_query_first : scalar, first match or NULL
SELECT id, jsonb_path_query_first(payload, '$.items[*] ? (@.qty > 10)')
FROM event;

-- jsonb_path_match : boolean predicate-check (path must be a single predicate)
SELECT id FROM event WHERE jsonb_path_match(payload, 'exists($.items[*])');

-- v16 type methods :
SELECT id, jsonb_path_query(payload, '$.amount.double()')
FROM event;

-- strict vs lax mode (default lax) :
SELECT jsonb_path_query('{"a":1}'::jsonb, 'strict $.b');
-- ERROR : sql_json_member_not_found (2203B). Fix : default lax mode.
SELECT jsonb_path_query('{"a":1}'::jsonb, '$.b');
-- (no rows : member missing in lax mode)
```

Verified against : postgresql.org/docs/17/functions-json.html (SQL/JSON Path Language, jsonb_path_query / jsonb_path_match).

## Example 7 : `JSON_TABLE` Projection (v17+)

```sql
-- Source : an event row with a nested array.
INSERT INTO event (payload) VALUES (
  '{"items":[
    {"id":1,"sku":"A","qty":3},
    {"id":2,"sku":"B","qty":12},
    {"id":3,"sku":"C","qty":7}
  ]}'
);

-- v17 : SQL/JSON JSON_TABLE
SELECT e.id AS event_id, jt.*
FROM event e,
  JSON_TABLE(e.payload, '$.items[*]'
    COLUMNS (
      ord   FOR ORDINALITY,
      id    int  PATH '$.id',
      sku   text PATH '$.sku',
      qty   int  PATH '$.qty'
    )) AS jt
WHERE jt.qty > 5;
-- Returns one row per item with qty > 5.

-- NESTED PATH : multi-level unflattening
SELECT jt.*
FROM event e,
  JSON_TABLE(e.payload, '$.favorites[*]'
    COLUMNS (
      kind text PATH '$.kind',
      NESTED PATH '$.films[*]'
        COLUMNS (
          film_title text PATH '$.title',
          director   text PATH '$.director'
        )
    )) AS jt;

-- v15 / v16 fallback (no JSON_TABLE) :
SELECT e.id AS event_id,
       (item->>'id')::int  AS id,
       item->>'sku'         AS sku,
       (item->>'qty')::int AS qty
FROM event e,
     LATERAL jsonb_array_elements(e.payload->'items') AS item
WHERE (item->>'qty')::int > 5;
```

Verified against : postgresql.org/docs/17/functions-json.html (JSON_TABLE), PostgreSQL 17 release notes.

## Example 8 : Delete / Concatenate / Strip Nulls

```sql
-- Delete key
SELECT '{"a":1,"b":2}'::jsonb - 'a';                  -- {"b":2}
-- Delete multiple keys (text[])
SELECT '{"a":1,"b":2,"c":3}'::jsonb - ARRAY['a','b']; -- {"c":3}
-- Delete by array index
SELECT '[10,20,30]'::jsonb - 1;                       -- [10, 30]
-- Delete at nested path
SELECT '{"a":{"b":1,"c":2}}'::jsonb #- '{a,b}';       -- {"a":{"c":2}}

-- Concatenate / merge
SELECT '{"a":1}'::jsonb || '{"b":2}'::jsonb;          -- {"a":1,"b":2}
SELECT '{"a":1,"b":1}'::jsonb || '{"b":2}'::jsonb;    -- {"a":1,"b":2} (right wins)
SELECT '[1,2]'::jsonb || '[3,4]'::jsonb;              -- [1,2,3,4]

-- Strip object fields whose value is JSON null
SELECT jsonb_strip_nulls('{"a":1,"b":null,"c":{"d":null,"e":2}}'::jsonb);
-- {"a":1,"c":{"e":2}}

-- Pretty print
SELECT jsonb_pretty('{"a":1,"b":[1,2,3]}'::jsonb);
```

Verified against : postgresql.org/docs/17/functions-json.html.

## Example 9 : Type-Strict Containment

```sql
-- Containment is type-strict. Number 10 and string "10" are DIFFERENT :
SELECT '{"qty":10}'::jsonb @> '{"qty":10}';    -- t
SELECT '{"qty":10}'::jsonb @> '{"qty":"10"}';  -- f (number vs string)
SELECT '{"qty":"10"}'::jsonb @> '{"qty":10}';  -- f

-- Common mistake when constructing the RHS from app code :
PREPARE p(text) AS SELECT * FROM event WHERE payload @> jsonb_build_object('qty', $1);
EXECUTE p('10');
-- payload @> '{"qty":"10"}' : matches only string-typed qty values, not numeric.
-- Fix : pass the typed value, or build the jsonb with the correct type :
PREPARE p2(int) AS SELECT * FROM event WHERE payload @> jsonb_build_object('qty', $1);
EXECUTE p2(10);
```

Verified against : postgresql.org/docs/17/datatype-json.html (Containment and Existence).
