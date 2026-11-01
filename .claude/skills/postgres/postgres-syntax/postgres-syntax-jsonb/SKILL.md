---
name: postgres-syntax-jsonb
description: >
  Use when querying JSON columns, picking the right GIN opclass, or extracting values from jsonb payloads.
  Prevents slow JSONB queries from missing GIN index, wrong opclass choice (jsonb_path_ops cannot answer key-existence queries), and using json type where jsonb belongs.
  Covers ->, ->>, #>, #>>, @>, <@, ?, ?&, ?|, jsonb_set, jsonb_path_query, jsonb_path_match (v12+), JSON_TABLE (v17+), GIN jsonb_ops vs jsonb_path_ops decision tree, JSONB containment query pattern.
  Keywords: jsonb, json, JSONB, GIN, jsonb_ops, jsonb_path_ops, jsonpath, jsonb_path_query, JSON_TABLE, containment, ->>, @>, extract json key, query nested json, jsonb is slow, missing index on jsonb, how to query json field, why does my jsonb query do seqscan, json vs jsonb which one, jsonb_set update key
license: MIT
compatibility: "Designed for Claude Code. Requires PostgreSQL 15, 16, or 17."
metadata:
  author: OpenAEC-Foundation
  version: "1.0"
---

# postgres-syntax-jsonb

## Quick Reference :

PostgreSQL has TWO JSON column types : `json` (stores the input text verbatim, reparses on every read, cannot be GIN-indexed for containment) and `jsonb` (stores a decomposed binary form, single parse on insert, supports GIN, B-tree, hash, subscript update). ALWAYS use `jsonb` unless lossless round-tripping of key order, whitespace, and duplicate keys is a documented requirement (it almost never is). The dominant operator set is `->` and `->>` for object/array field access (returns jsonb or text respectively), `#>` and `#>>` for nested path access, and `@>` for containment ("does the left payload contain the right one?"). The dominant index is a GIN index ; the choice of opclass between default `jsonb_ops` and `jsonb_path_ops` is the highest-leverage performance decision : `jsonb_path_ops` is 3-5x smaller and faster for pure `@>` containment queries but DOES NOT support the key-existence operators `?`, `?|`, `?&`.

For value extraction inside SELECT/WHERE, `->>` (text) is what callers usually want : `WHERE doc->>'status' = 'paid'`. This is NOT GIN-indexable as-is : if the same predicate runs a lot, add a B-tree expression index on `((doc->>'status'))`. For schemaless filtering by shape, `WHERE doc @> '{"status":"paid"}'` is the GIN-friendly form. v12 added `jsonpath` (operators `@?` and `@@`, functions `jsonb_path_query` and `jsonb_path_match`). v17 added `JSON_TABLE` for relational projection from JSON arrays and `JSON_EXISTS / JSON_QUERY / JSON_VALUE` SQL/JSON functions.

## When To Use This Skill :

ALWAYS use this skill when :
- Choosing between `json` and `jsonb` column types
- Writing queries that extract values from JSON columns (`->`, `->>`, `#>`, `#>>`)
- Filtering by JSON structure (`@>` containment, `?` key existence, `?|` / `?&` set existence)
- Picking a GIN opclass for a JSONB column (`jsonb_ops` vs `jsonb_path_ops`)
- Updating a JSONB column in place (`jsonb_set`, `jsonb_insert`, subscript update v14+)
- Using `jsonpath` to express complex queries (`@?`, `@@`, `jsonb_path_query`, `jsonb_path_match`)
- Projecting JSON arrays to relational rows (`JSON_TABLE`, v17+)

NEVER use this skill for :
- Index strategy across types : see `postgres-core-indexing-strategy`
- GIN indexing of arrays / tsvector / pg_trgm : see `postgres-core-indexing-strategy`
- JSON validation / schema enforcement via check constraints : see `postgres-impl-validation-patterns`
- Performance tuning beyond GIN choice : see `postgres-impl-explain-analyze`

## Decision Trees :

### `json` or `jsonb`? :

```
Need to preserve exact input bytes (whitespace, key order, duplicate keys)?
├── Yes (very rare ; usually for cryptographic signature verification) :
│       json type ; accept that containment cannot be indexed.
└── No (the normal case) : jsonb. Faster on read, indexable, subscript update.
```

### Pick GIN opclass : `jsonb_ops` or `jsonb_path_ops`? :

```
Will queries ever use key-existence operators ?, ?|, ?& ?
├── Yes : default `jsonb_ops`.
│         CREATE INDEX ON t USING GIN (doc);
│         Larger index (~3-5x), supports @>, ?, ?|, ?&, @?, @@.
└── No : containment-only `jsonb_path_ops`.
          CREATE INDEX ON t USING GIN (doc jsonb_path_ops);
          3-5x smaller, faster lookup, supports @>, @?, @@ ONLY.
          Better for stable schemas where you always filter by shape.
```

### Extract a value vs filter by shape : which operator? :

```
SELECT list : need the VALUE of a JSON key
├── As JSON (preserve nested structure) : doc->'key' or doc#>'{nested,key}'
└── As text (scalar value) : doc->>'key' or doc#>>'{nested,key}'

WHERE clause : need to FILTER rows
├── Filter by SHAPE (key+value match, anywhere or at root depending on form) :
│       WHERE doc @> '{"key":"value"}'   -- GIN-friendly
├── Filter by KEY EXISTENCE :
│       WHERE doc ? 'key'                -- GIN-friendly (jsonb_ops only)
│       WHERE doc ?| array['a','b']      -- any of these keys
│       WHERE doc ?& array['a','b']      -- all of these keys
├── Filter by EXTRACTED SCALAR :
│       WHERE doc->>'status' = 'paid'    -- NOT GIN-indexed by default ;
│                                          add B-tree on ((doc->>'status'))
└── Filter by EXPRESSION over path :
        WHERE doc @? '$.items[*] ? (@.qty > 10)'  -- jsonpath, v12+
```

### Update inside a JSONB column : `jsonb_set` / `jsonb_insert` / subscript? :

```
PostgreSQL version >= 14 AND the path is shallow (depth ≤ 3) and known ?
├── Yes : subscript syntax (concise, SQL-standard-ish).
│         UPDATE t SET doc['status'] = '"paid"' WHERE id = 42;
└── No : functions.
    ├── Modify existing key OR create missing key (default) : jsonb_set
    │       UPDATE t SET doc = jsonb_set(doc, '{status}', '"paid"');
    ├── Insert into array WITHOUT replacing : jsonb_insert
    │       UPDATE t SET doc = jsonb_insert(doc, '{items, 0}', '{"sku":"x"}', false);
    └── Strip null leaves before storing : jsonb_strip_nulls
            UPDATE t SET doc = jsonb_strip_nulls(doc);
```

## Patterns :

### Pattern 1 : Always declare JSON columns as `jsonb`

ALWAYS use `jsonb`, not `json`, for any column you intend to query.
NEVER store JSON in a `text` column "and parse it in the app" : you lose every benefit (indexing, validation, operators).

```sql
CREATE TABLE event (
  id         bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  occurred_at timestamptz NOT NULL DEFAULT now(),
  payload    jsonb       NOT NULL
);

-- jsonb validates the input on insert (raises 22P02 invalid_text_representation
-- on malformed JSON) and stores a normalized binary form :
INSERT INTO event (payload) VALUES ('{"kind":"signup","user_id":42}');

-- Same insert on a json column accepts the input but reparses on EVERY read.
-- Same insert on a text column accepts anything, validates nothing.
```

WHY : `jsonb` parses once at INSERT into a decomposed binary form (no whitespace, normalized key order, no duplicate keys : last value wins). All subsequent reads are O(1) per key lookup. `json` is stored as the input text and reparsed on every access. The performance gap widens with depth and document size. Source : postgresql.org/docs/17/datatype-json.html (JSON Types).

### Pattern 2 : Extract values with `->`, `->>`, `#>`, `#>>`

ALWAYS pick the operator by RETURN TYPE you need : `->` and `#>` return `jsonb`, `->>` and `#>>` return `text`.
NEVER cast `->` to text manually (`(doc->'k')::text` includes JSON quotes for string values) : use `->>` instead.

```sql
SELECT
  doc->'user'                AS user_subdoc,    -- jsonb : {"id":42,"name":"Alice"}
  doc->>'user'               AS user_subdoc_text, -- text : '{"id":42,"name":"Alice"}'
  doc->'user'->>'name'       AS user_name,      -- text : 'Alice'
  doc->'tags'->0             AS first_tag,      -- jsonb : "x" (with quotes)
  doc->>'tags'->>0           AS WRONG,          -- error : -> on text
  doc->'tags'->>0            AS first_tag_text, -- text : x (no quotes)
  doc#>'{user, addr, city}'  AS city_subdoc,    -- jsonb
  doc#>>'{user, addr, city}' AS city_text       -- text
FROM event;
```

Common mistake : `(doc->'name')::text` returns `'"Alice"'` (WITH the JSON quotes). `doc->>'name'` returns `'Alice'`. Always prefer `->>` when you want a string value.

WHY : `->` preserves the JSON type system inside the result so you can chain (`->`...`->`...`->>`). `->>` projects to text exactly once at the end. Mixing them or casting manually gives subtle string-quoting bugs. Source : postgresql.org/docs/17/functions-json.html (jsonb operators table).

### Pattern 3 : GIN index for containment filtering

ALWAYS create a GIN index when `WHERE doc @> '...'` queries run often.
NEVER expect `WHERE doc @> '...'` to use a B-tree index : it cannot.

```sql
-- Default GIN opclass (jsonb_ops) : supports @>, ?, ?|, ?&, @?, @@
CREATE INDEX event_payload_gin ON event USING GIN (payload);

-- Containment-only opclass (jsonb_path_ops) : 3-5x smaller, faster @>
CREATE INDEX event_payload_path_gin ON event USING GIN (payload jsonb_path_ops);
-- TRADEOFF : ? / ?| / ?& key-existence queries fall back to seq scan.

-- The query :
SELECT * FROM event WHERE payload @> '{"kind":"signup"}';
-- Both index variants serve this. The planner picks one.

-- Force inspection :
EXPLAIN (ANALYZE, BUFFERS) SELECT count(*) FROM event WHERE payload @> '{"kind":"signup"}';
-- Expect : Bitmap Index Scan on event_payload_gin (or _path_gin).
```

WHY : `@>` containment is the operator GIN was designed for. Without the index PostgreSQL sequentially scans every row and tests containment in CPU. With it, the bitmap-index-scan resolves matching tids in millisecond time on tables with millions of rows. Source : postgresql.org/docs/17/datatype-json.html (jsonb Indexing).

### Pattern 4 : Pick `jsonb_path_ops` when queries are containment-only

ALWAYS prefer `jsonb_path_ops` if every JSONB query in the application is a `@>` containment.
NEVER use `jsonb_path_ops` if any query uses `?`, `?|`, or `?&` : those operators are NOT in the opclass and will silently seqscan.

```sql
-- Stable schema : payload always has the same shape, queries always filter
-- by full-shape equality
CREATE TABLE order_log (
  id      bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  payload jsonb  NOT NULL
);

CREATE INDEX order_log_payload_gin ON order_log USING GIN (payload jsonb_path_ops);

-- Served by index :
SELECT * FROM order_log WHERE payload @> '{"status":"paid","region":"eu"}';

-- NOT served by index (key-existence is NOT in jsonb_path_ops) :
SELECT * FROM order_log WHERE payload ? 'refund';
-- Will seqscan. Fix : also add a default-opclass index, or use @> '{"refund":...}'.
```

Index size comparison (typical, your mileage varies) :

| Opclass | Index size | Supports | Notes |
|---|---|---|---|
| `jsonb_ops` (default) | 100% (baseline) | `@>`, `?`, `?|`, `?&`, `@?`, `@@` | Flexible |
| `jsonb_path_ops` | ~20-30% of baseline | `@>`, `@?`, `@@` ONLY | Smaller + faster `@>` |

Source : postgresql.org/docs/17/datatype-json.html (jsonb Indexing : "The technical difference between a jsonb_ops and a jsonb_path_ops GIN index is that the former creates independent index items for each key and value in the data, while the latter creates index items only for each value").

### Pattern 5 : B-tree expression index for `->>` predicates

ALWAYS create a B-tree expression index on `(doc->>'key')` when the same `WHERE doc->>'key' = ?` predicate runs often.
NEVER expect a GIN index to serve `doc->>'key' = ?` : `->>` is not in GIN's operator class for jsonb.

```sql
-- For frequent SELECT ... WHERE payload->>'status' = 'paid' :
CREATE INDEX event_status_btree ON event ((payload->>'status'));

-- Now this is a plain B-tree index scan :
EXPLAIN SELECT count(*) FROM event WHERE payload->>'status' = 'paid';

-- Multi-key compound predicate : expression index on the tuple
CREATE INDEX event_status_region_btree
  ON event ((payload->>'status'), (payload->>'region'));

-- Partial index for selective constant-key queries :
CREATE INDEX event_unpaid_idx
  ON event ((payload->>'created_at'))
  WHERE payload->>'status' = 'unpaid';
```

WHY : `->>` extracts a text scalar. B-tree handles equality and range on text. GIN handles multi-value containment, not scalar equality. The expression-index pattern is the bridge from "schemaless JSON column" to "indexable column-like behavior" for hot keys. Source : postgresql.org/docs/17/indexes-expressional.html, postgresql.org/docs/17/datatype-json.html.

### Pattern 6 : Update in place with `jsonb_set` / subscript

ALWAYS use `jsonb_set` (or v14+ subscript) to update individual JSON keys. NEVER serialize the whole document in the application and `UPDATE ... SET doc = $1` : you race against concurrent writers.

```sql
-- jsonb_set(target, path, new_value, create_missing default true)
UPDATE event SET payload = jsonb_set(payload, '{status}', '"paid"')
  WHERE id = 42;

-- Nested path (text[]):
UPDATE event SET payload = jsonb_set(payload, '{user, addr, city}', '"Amsterdam"')
  WHERE id = 42;

-- Create-missing toggle : refuse to create the key if absent
UPDATE event SET payload = jsonb_set(payload, '{coupon}', '"X10"', false)
  WHERE id = 42;
-- If payload had no "coupon" key, payload is returned unchanged.

-- jsonb_insert : array-aware sibling that does NOT overwrite
UPDATE event SET payload = jsonb_insert(payload, '{items, 0}', '{"sku":"X"}', false)
  WHERE id = 42;
-- insert_after=false inserts BEFORE position 0 (i.e. prepend).

-- v14+ subscript update (concise) :
UPDATE event SET payload['status'] = '"paid"' WHERE id = 42;
UPDATE event SET payload['user']['addr']['city'] = '"Amsterdam"' WHERE id = 42;
-- Note: RHS must be a valid JSON scalar/object/array literal, including quotes
-- around strings. Padding behavior : assigning to array index 2 of [] yields
-- [null, null, 2].
```

WHY : `jsonb_set` and subscript update operate ON the existing row inside the SQL engine, so concurrent UPDATEs on different keys do not lose each other. Read-modify-write in application code overwrites whatever the other session just stored. Source : postgresql.org/docs/17/functions-json.html (jsonb_set, jsonb_insert), postgresql.org/docs/17/datatype-json.html (jsonb Subscripting).

### Pattern 7 : Query nested structure with jsonpath (`@?`, `@@`, `jsonb_path_query`)

ALWAYS use jsonpath for queries that need filters inside arrays or recursive descent.
NEVER chain `->` / `->>` to drill into arrays of unknown length : jsonpath expresses it concisely.

```sql
-- jsonpath operators (v12+) :
-- doc @? '$.path'    : true if at least one match exists
-- doc @@ '$.predicate' : true if predicate evaluates true

-- Find events where any item has qty > 10 :
SELECT id FROM event WHERE payload @? '$.items[*] ? (@.qty > 10)';

-- Predicate form :
SELECT id FROM event WHERE payload @@ '$.items[*].qty > 10';

-- Materialize all matching values :
SELECT id, jsonb_path_query(payload, '$.items[*] ? (@.qty > 10)') AS hot_item
FROM event;

-- Variables (third argument is a jsonb context object, referenced via $name) :
SELECT jsonb_path_query(payload, '$.items[*] ? (@.qty > $min)',
                        '{"min":10}')
FROM event;

-- Boolean predicate-only :
SELECT jsonb_path_match(payload, 'exists($.items[*] ? (@.qty > $min))',
                        '{"min":10}')
FROM event;
```

jsonpath grammar (cheat sheet) :

| Form | Meaning |
|---|---|
| `$` | The root document |
| `$.key` | Object member `key` |
| `$.*` | All object members at this level |
| `$.**` | Recursive descent (PostgreSQL extension) |
| `$[idx]` | Array index `idx` (negative counts from end) |
| `$[start to end]` | Array slice |
| `$[*]` | All array elements |
| `$[last]` | Last array element |
| `? (@.x > 0)` | Filter expression ; `@` is the current item |
| `.bigint()`, `.boolean()`, `.date()`, `.timestamp()` | Type methods (v16+) |

Source : postgresql.org/docs/17/functions-json.html (SQL/JSON Path Language), postgresql.org/docs/17/datatype-json.html.

### Pattern 8 : Project JSON arrays to rows with `JSON_TABLE` (v17+)

ALWAYS use `JSON_TABLE` on v17+ when you need to relationalize a JSON array.
NEVER hand-roll a `LATERAL jsonb_to_recordset(...)` query when `JSON_TABLE` is available : it is the SQL-standard form.

```sql
-- v17 only :
SELECT jt.id, jt.kind, jt.title
FROM event,
  JSON_TABLE(payload, '$.items[*]'
    COLUMNS (
      id    int  PATH '$.id',
      kind  text PATH '$.kind',
      title text PATH '$.title'
    )) AS jt;

-- NESTED PATH unflattens nested arrays :
SELECT jt.*
FROM event,
  JSON_TABLE(payload, '$.favorites[*]'
    COLUMNS (
      kind text PATH '$.kind',
      NESTED PATH '$.films[*]'
        COLUMNS (
          title text PATH '$.title',
          dir   text PATH '$.director'
        )
    )) AS jt;
```

For v15 / v16 (no `JSON_TABLE`), use `jsonb_to_recordset` or `LATERAL jsonb_array_elements` :

```sql
-- v15 / v16 compatible :
SELECT e.id, item->>'kind' AS kind, item->>'title' AS title
FROM event e, LATERAL jsonb_array_elements(e.payload->'items') AS item;
```

Source : postgresql.org/docs/17/functions-json.html (JSON_TABLE), PostgreSQL 17 release notes.

## Anti-Patterns :

(One-liners ; full diagnosis + fix in `references/anti-patterns.md`.)

- Using `json` type when queries are needed : reparses on every read, no GIN containment. Fix : `jsonb`.
- Missing GIN index on a column with frequent `@>` filters : seqscan on every query. Fix : `CREATE INDEX ... USING GIN (col)`.
- `jsonb_path_ops` opclass on a column that uses `?`, `?|`, `?&` : silent seqscan for those operators. Fix : default `jsonb_ops` opclass, or also add expression indexes.
- B-tree expression index used for `WHERE doc->>'k' = ?` but predicate uses LIKE/range : the index serves only the equality form. Fix : add the right form, or use jsonpath.
- `(doc->'key')::text` to get a string value : returns the value WITH JSON quotes. Fix : `doc->>'key'`.
- Read-modify-write of an entire jsonb document in the application : races with concurrent writers. Fix : `jsonb_set` or subscript update.
- `jsonb_set(..., create_missing := true)` (default) when you intended "update if exists" only : silently creates keys. Fix : explicit `false` argument.
- `WHERE doc @> '{}' AND ...` : matches every non-null row, GIN bitmap is the whole table. Fix : drop the empty-object containment, use `doc IS NOT NULL` if that is what you meant.
- Comparing `doc @> '{"qty":10}'` when `qty` is stored as a JSON number but you wrote the literal as a string (`'{"qty":"10"}'`) : containment is type-strict, never matches. Fix : write the literal with correct JSON type.
- Indexing `(doc->>'massive_array_field')` with B-tree : index entries can exceed max tuple size. Fix : GIN on a scalar-projection or hash a digest.

## Reference Links :

- [references/methods.md](references/methods.md) : Full operator table (return type + supported by which GIN opclass), full function table (jsonb_set, jsonb_insert, jsonb_path_query, etc.), jsonpath grammar reference, version annotations.
- [references/examples.md](references/examples.md) : Verified end-to-end examples for every operator and function, GIN opclass tradeoff with size measurements, JSON_TABLE walk-through.
- [references/anti-patterns.md](references/anti-patterns.md) : Each anti-pattern with symptom, detection query, fix, and SQLSTATE where applicable.

## See Also :

- `postgres-core-indexing-strategy` : GIN access method overview across data types, partial / expression indexes
- `postgres-syntax-arrays-ranges` : array operators + range types (also GIN-indexable)
- `postgres-core-version-matrix` : v17 JSON_TABLE / JSON_VALUE / JSON_EXISTS, v16 jsonpath type methods
- `postgres-impl-explain-analyze` : verifying that the planner picks the GIN index
- Vooronderzoek section : §3 (Type System + JSON/JSONB), §4 (Indexing Strategy), §19 anti-patterns 4, 8, 15
- Official docs : https://www.postgresql.org/docs/17/datatype-json.html, https://www.postgresql.org/docs/17/functions-json.html, https://www.postgresql.org/docs/17/indexes-types.html
