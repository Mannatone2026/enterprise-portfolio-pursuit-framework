# Methods : JSONB Operators, Functions, Indexing

Full reference tables for the operators, functions, jsonpath grammar, and GIN opclasses used in `postgres-syntax-jsonb`. All entries verified against postgresql.org/docs/17 (datatype-json.html, functions-json.html).

## JSON vs JSONB Storage

| Aspect | `json` | `jsonb` |
|---|---|---|
| Storage form | Verbatim text | Decomposed binary |
| Parse cost on read | Every read | Zero |
| Whitespace + key order | Preserved | Normalized away |
| Duplicate keys | Preserved (all kept) | Collapsed (last wins) |
| Comparable (`=`, `<`, `>`) | No | Yes (well-defined) |
| GIN containment indexable | No | Yes |
| B-tree / hash indexable | Whole value only | Yes (whole value) |
| Subscript update (v14+) | No | Yes |
| Use case | Cryptographic round-trip | Everything else |

Source : postgresql.org/docs/17/datatype-json.html (JSON Types).

## JSONB Operators Table

| Operator | Left | Right | Result | Description | GIN-supported? |
|---|---|---|---|---|---|
| `->` | jsonb | text | jsonb | Object member | No (use `@>` instead) |
| `->` | jsonb | int | jsonb | Array element (negative counts from end) | No |
| `->>` | jsonb | text | text | Object member as text | No |
| `->>` | jsonb | int | text | Array element as text | No |
| `#>` | jsonb | text[] | jsonb | Nested path | No |
| `#>>` | jsonb | text[] | text | Nested path as text | No |
| `@>` | jsonb | jsonb | boolean | Left contains right | YES (both opclasses) |
| `<@` | jsonb | jsonb | boolean | Left contained in right | YES (`jsonb_ops` only) |
| `?` | jsonb | text | boolean | Key/element exists at top level | YES (`jsonb_ops` only) |
| `?|` | jsonb | text[] | boolean | Any of the keys exist | YES (`jsonb_ops` only) |
| `?&` | jsonb | text[] | boolean | All keys exist | YES (`jsonb_ops` only) |
| `\|\|` | jsonb | jsonb | jsonb | Concatenate (object merge ; array concat ; scalar wraps to array) | No |
| `-` | jsonb | text | jsonb | Delete key from object | No |
| `-` | jsonb | int | jsonb | Delete array element by index | No |
| `-` | jsonb | text[] | jsonb | Delete multiple keys (v10+) | No |
| `#-` | jsonb | text[] | jsonb | Delete at path | No |
| `@?` | jsonb | jsonpath | boolean | jsonpath match exists (v12+) | YES (both opclasses) |
| `@@` | jsonb | jsonpath | boolean | jsonpath predicate is true (v12+) | YES (both opclasses) |

Source : postgresql.org/docs/17/functions-json.html (Table 9.46 jsonb Operators).

## JSONB Path Functions

| Function | Signature | Description | Version |
|---|---|---|---|
| `jsonb_set` | `jsonb_set(target jsonb, path text[], new_value jsonb [, create_if_missing boolean DEFAULT true]) → jsonb` | Replace value at path ; create if missing when flag true | v9.5+ |
| `jsonb_set_lax` | `jsonb_set_lax(target jsonb, path text[], new_value jsonb [, create_if_missing boolean DEFAULT true [, null_value_treatment text DEFAULT 'use_json_null']]) → jsonb` | Like jsonb_set, but configurable NULL handling | v13+ |
| `jsonb_insert` | `jsonb_insert(target jsonb, path text[], new_value jsonb [, insert_after boolean DEFAULT false]) → jsonb` | Insert ; if path is array element, before (default) or after ; if object field, only when key absent | v9.6+ |
| `jsonb_path_query` | `jsonb_path_query(target jsonb, path jsonpath [, vars jsonb [, silent boolean]]) → setof jsonb` | All matches as set | v12+ |
| `jsonb_path_query_array` | `(target, path [, vars [, silent]]) → jsonb` | All matches wrapped in JSON array | v12+ |
| `jsonb_path_query_first` | `(target, path [, vars [, silent]]) → jsonb` | First match or NULL | v12+ |
| `jsonb_path_match` | `jsonb_path_match(target jsonb, path jsonpath [, vars jsonb [, silent boolean]]) → boolean` | SQL boolean of predicate-check expression | v12+ |
| `jsonb_path_exists` | `(target, path [, vars [, silent]]) → boolean` | True if at least one match | v12+ |
| `jsonb_strip_nulls` | `jsonb_strip_nulls(jsonb) → jsonb` | Recursively remove object fields whose value is JSON null | v9.5+ |
| `jsonb_pretty` | `jsonb_pretty(jsonb) → text` | Human-readable indented form | v9.5+ |
| `jsonb_typeof` | `jsonb_typeof(jsonb) → text` | One of 'object','array','string','number','boolean','null' | v9.4+ |
| `jsonb_array_length` | `jsonb_array_length(jsonb) → int` | Length of top-level JSON array | v9.4+ |
| `jsonb_object_keys` | `jsonb_object_keys(jsonb) → setof text` | Keys at top level | v9.4+ |
| `jsonb_each` | `jsonb_each(jsonb) → setof (key text, value jsonb)` | Top-level key/value pairs | v9.4+ |
| `jsonb_each_text` | `jsonb_each_text(jsonb) → setof (key text, value text)` | Same with text value | v9.4+ |
| `jsonb_array_elements` | `jsonb_array_elements(jsonb) → setof jsonb` | Each element of top-level array | v9.4+ |
| `jsonb_array_elements_text` | `jsonb_array_elements_text(jsonb) → setof text` | Same as text | v9.4+ |
| `jsonb_to_record` | `jsonb_to_record(jsonb) AS (col1 type, ...)` | Project object into a record | v9.4+ |
| `jsonb_to_recordset` | `jsonb_to_recordset(jsonb) AS (col1 type, ...)` | Project array-of-objects into rows | v9.4+ |
| `JSON_TABLE` | `JSON_TABLE(context, path COLUMNS (...))` | SQL/JSON projection to relational rows | v17+ |
| `JSON_EXISTS` | `JSON_EXISTS(context, path)` | SQL/JSON existence predicate | v17+ |
| `JSON_QUERY` | `JSON_QUERY(context, path)` | SQL/JSON value extraction (jsonb) | v17+ |
| `JSON_VALUE` | `JSON_VALUE(context, path)` | SQL/JSON value extraction (text scalar) | v17+ |
| `JSON_ARRAY` | `JSON_ARRAY(value1, value2, ...)` | Construct array | v16+ |
| `JSON_OBJECT` | `JSON_OBJECT(key1 VALUE val1, ...)` | Construct object | v16+ |
| `JSON_ARRAYAGG` | aggregate | Aggregate values into JSON array | v16+ |
| `JSON_OBJECTAGG` | aggregate | Aggregate key/value pairs into JSON object | v16+ |

Source : postgresql.org/docs/17/functions-json.html.

## jsonpath Grammar

| Form | Meaning | Example |
|---|---|---|
| `$` | Context root | `$.name` |
| `$.key` | Object member | `$.user.name` |
| `$.*` | All members at level | `$.* ? (@ > 0)` |
| `$.**` | Recursive descent (PG extension) | `$.**.price` |
| `$[idx]` | Array index | `$.items[0]` |
| `$[start to end]` | Array slice | `$.items[0 to 4]` |
| `$[*]` | All array elements | `$.items[*]` |
| `$[last]` | Last element | `$.items[last]` |
| `? (predicate)` | Filter ; `@` is current item | `$.items[*] ? (@.qty > 10)` |
| `$varname` | Variable from `vars` argument | `$.items[*] ? (@.qty > $min)` |
| `\|\|` `&&` `!` | Boolean operators in predicates | `? (@.a > 0 && @.b < 10)` |
| `like_regex` | Regex match (POSIX) | `? (@ like_regex "^foo")` |
| `starts with` | String prefix | `? (@ starts with "foo")` |
| `.type()` | Type of the item (returns text) | `$.x.type()` |
| `.size()` | Number of items / array length | `$.items.size()` |
| `.double()`, `.bigint()`, `.boolean()`, `.date()`, `.timestamp()` | Type conversion methods (v16+) | `$.x.bigint()` |
| `is unknown` | Three-valued logic predicate | `? ((@.x > 0) is unknown)` |
| `exists(path)` | Existence in predicate | `exists($.items[*] ? (@.x > 0))` |

Strict vs lax mode (default lax) : `strict $.x.y` raises an error if `.y` is missing ; `lax $.x.y` returns nothing. Prefix the path with `strict` or `lax`.

Source : postgresql.org/docs/17/functions-json.html (SQL/JSON Path Language).

## GIN Opclasses for jsonb

| Opclass | Indexes | Index size | Supports |
|---|---|---|---|
| `jsonb_ops` (default) | Every key + every value as separate entries | Baseline | `@>`, `<@`, `?`, `?|`, `?&`, `@?`, `@@` |
| `jsonb_path_ops` | Hashes of key-path + value pairs | ~20-30% of baseline (typical) | `@>`, `@?`, `@@` only |

The `jsonb_path_ops` opclass produces no entries for `{}` (empty object) or `[]` (empty array). Containment queries that target empty structures fall back to a bitmap-scan of the whole index ; consider `coalesce(jsonb_array_length(col), 0) > 0` or similar in WHERE to prevent.

Build a GIN index :

```sql
CREATE INDEX t_doc_gin      ON t USING GIN (doc);                    -- jsonb_ops
CREATE INDEX t_doc_path_gin ON t USING GIN (doc jsonb_path_ops);     -- containment-only
```

Build CONCURRENTLY on a live table :

```sql
CREATE INDEX CONCURRENTLY t_doc_gin ON t USING GIN (doc);
```

Maintenance :

```sql
REINDEX INDEX CONCURRENTLY t_doc_gin;        -- v12+
```

Source : postgresql.org/docs/17/datatype-json.html (jsonb Indexing).

## Subscript Update (v14+)

Subscript syntax allows in-place modification of JSON keys without `jsonb_set` :

```sql
UPDATE t SET doc['key']                 = '"value"';
UPDATE t SET doc['nested']['key']       = '42';
UPDATE t SET doc['arr'][2]              = 'true';
```

The right-hand side MUST be a valid JSON literal expression. Strings need surrounding double quotes inside the SQL string : `'"value"'`. Padding behavior : assigning index 2 to an empty array yields `[null, null, 2]` (the gaps are JSON null). Subscript update on `json` (not `jsonb`) is NOT supported.

Source : postgresql.org/docs/17/datatype-json.html (jsonb Subscripting).

## SQLSTATE for JSON Errors

| Code | Symbol | When raised |
|---|---|---|
| `22P02` | invalid_text_representation | Invalid JSON syntax at INSERT or `::jsonb` cast |
| `22023` | invalid_parameter_value | `jsonb_set` path argument invalid |
| `2203A` | sql_json_array_not_found | jsonpath addresses non-array in strict mode |
| `2203B` | sql_json_member_not_found | jsonpath addresses missing member in strict mode |
| `2203E` | sql_json_number_not_found | numeric coercion fails on non-number value |
| `2203F` | sql_json_object_not_found | jsonpath addresses non-object in strict mode |

Source : postgresql.org/docs/17/errcodes-appendix.html (Class 22, Class 2203 SQL/JSON Errors).
