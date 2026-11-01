# Methods : LATERAL Reference

Syntax forms, keyword rules, and planner behavior for `postgres-syntax-lateral-joins`. All facts verified against postgresql.org/docs/17/queries-table-expressions.html.

## Syntax Forms

| Form | Meaning | Behavior on empty inner result |
|---|---|---|
| `FROM outer, LATERAL (SELECT ...) sub` | Implicit CROSS JOIN with LATERAL | Outer row dropped |
| `FROM outer CROSS JOIN LATERAL (SELECT ...) sub` | Same as above, explicit | Outer row dropped |
| `FROM outer INNER JOIN LATERAL (SELECT ...) sub ON true` | Same as above, ON clause is `true` | Outer row dropped |
| `FROM outer LEFT JOIN LATERAL (SELECT ...) sub ON true` | Outer LEFT JOIN ; sub-columns NULL when inner empty | Outer row kept |
| `FROM outer, LATERAL function_call(outer.col) sub` | Set-returning function, LATERAL keyword optional | Outer row dropped if function emits zero rows |
| `FROM outer LEFT JOIN LATERAL function_call(outer.col) sub ON true` | Set-returning function with outer preservation | Outer row kept |

All forms support multiple LATERAL items in the same FROM clause, including chained LATERALs that reference each other (left-to-right). Each LATERAL item REQUIRES an alias (`AS sub`).

Source : postgresql.org/docs/17/queries-table-expressions.html (LATERAL Subqueries).

## When Is the LATERAL Keyword Required?

| FROM item shape | Keyword required? | Notes |
|---|---|---|
| `(SELECT ...) sub` referencing prior FROM items | YES | Without it : `42P01` "missing FROM-clause entry" |
| `(SELECT ...) sub` NOT referencing prior items | No | LATERAL is harmless but redundant |
| `function_call(args)` with args referencing prior items | NO (optional) | Function arguments always allowed to reference prior FROM items |
| `function_call(args)` with no outer reference | No | Trivial set-returning function call |
| `ROWS FROM(f1(...), f2(...))` with outer arg refs | No (optional) | Same as single function call |

The keyword is sometimes written even when optional to make the cross-reference explicit. This is a style choice, not a correctness requirement.

Source : postgresql.org/docs/17/queries-table-expressions.html ("Table functions appearing in FROM can also be preceded by the key word LATERAL, but for functions the key word is optional ; the function's arguments can contain references to columns provided by preceding FROM items in any case.").

## Set-Returning Functions Commonly Used With LATERAL

| Function | Result row shape | Typical LATERAL use case |
|---|---|---|
| `unnest(anyarray)` | one row per array element | Expand `text[]` / `int[]` per row |
| `generate_series(start, stop [, step])` | one row per value | Time bucketing, gap fill |
| `generate_subscripts(array, dim)` | one row per index | Numbered iteration over array |
| `jsonb_array_elements(jsonb)` | one row per JSON array element (as jsonb) | Flatten JSON array column |
| `jsonb_array_elements_text(jsonb)` | one row per element (as text) | Same, when element is scalar text |
| `jsonb_each(jsonb)` | one row per key/value pair | Flatten JSON object column |
| `jsonb_each_text(jsonb)` | one row per pair, value as text | Same with text projection |
| `jsonb_to_recordset(jsonb)` | one row per array element with named columns | Schema-aware JSON expansion |
| `regexp_matches(string, pattern, flags)` | one row per match | Per-row regex extraction |
| `regexp_split_to_table(string, pattern)` | one row per split fragment | Tokenize column per row |
| `string_to_table(string, delim)` (v14+) | one row per fragment | CSV-style splitting |
| Custom PL/pgSQL function returning `SETOF row_type` | one row per RETURN NEXT | Per-row business logic |

Source : postgresql.org/docs/17/functions-srf.html, postgresql.org/docs/17/functions-json.html.

## Equivalence Table

| Hand-rolled form | LATERAL form | Differs in? |
|---|---|---|
| `SELECT ..., (SELECT count(*) FROM b WHERE b.a_id = a.id) FROM a` (correlated scalar) | `SELECT ..., x.cnt FROM a, LATERAL (SELECT count(*) AS cnt FROM b WHERE b.a_id = a.id) x` | Same plan typically ; LATERAL supports MULTI-column return |
| `SELECT ... FROM a CROSS JOIN b WHERE b.a_id = a.id` | `SELECT ... FROM a, LATERAL (SELECT * FROM b WHERE b.a_id = a.id) x` | Same plan ; equality push-down. LATERAL is no-op here |
| `SELECT row_number() OVER (PARTITION BY a_id ORDER BY t DESC) rn FROM b ... WHERE rn <= 3` | `SELECT ... FROM a, LATERAL (SELECT * FROM b WHERE b.a_id = a.id ORDER BY t DESC LIMIT 3) x` | LATERAL uses index-range-scan per outer row ; window scans all inner rows |
| `SELECT a.*, x.* FROM a LEFT JOIN x ON x.a_id = a.id WHERE ...` (no per-row LIMIT) | `SELECT a.*, x.* FROM a LEFT JOIN LATERAL (SELECT * FROM x WHERE x.a_id = a.id LIMIT 1) x ON true` | LATERAL form picks exactly one inner row per outer ; LEFT JOIN form yields all matching inner rows |

Source : postgresql.org/docs/17/queries-table-expressions.html, postgresql.org/docs/17/queries-subqueries.html.

## Planner Behavior With LATERAL

| Plan node | When the planner uses it |
|---|---|
| `Nested Loop` | Default for LATERAL : evaluate inner subquery once per outer row |
| `Nested Loop ... -> Index Scan / Index Only Scan` | LATERAL with WHERE on indexed column on inner side ; the optimal shape |
| `Nested Loop Left Join` | LATERAL inside `LEFT JOIN LATERAL ... ON true` |
| `Hash Join` | Rarely used for LATERAL : only when planner can pull the LATERAL up into a regular join |
| `Memoize` (v14+) | Wraps the Nested Loop's inner side with per-row caching when outer values repeat |

Verify with `EXPLAIN (ANALYZE, BUFFERS) ...` : look for `Nested Loop -> Index Scan using <parent_id_orderby_idx>` to confirm the inner-side index is being used per outer row.

Source : postgresql.org/docs/17/using-explain.html, postgresql.org/docs/17/queries-table-expressions.html.

## Restrictions

| Restriction | Source |
|---|---|
| LATERAL subquery cannot reference items to its RIGHT in the FROM clause | "it can also refer to any items that are on the left-hand side of a JOIN that it is on the right-hand side of" |
| LATERAL subquery alias is MANDATORY | parser rule for FROM subqueries |
| Cannot use LATERAL on a view that itself uses LATERAL on outer columns the view's caller does not have | view-resolution rule |
| LATERAL inside CTE : the CTE column scope is established before LATERAL evaluation ; same rules apply | postgresql.org/docs/17/queries-with.html |
| LATERAL is incompatible with `FULL OUTER JOIN` (cannot symmetrically preserve both sides) | parser rule |

Source : postgresql.org/docs/17/queries-table-expressions.html.

## SQLSTATE Reference

| SQLSTATE | Symbol | When raised |
|---|---|---|
| `42P01` | undefined_table | Subquery references outer FROM item without LATERAL keyword |
| `42601` | syntax_error | LATERAL subquery without alias |
| `42P10` | invalid_column_reference | LATERAL references item to its right |
| `42703` | undefined_column | Outer column name typo inside LATERAL subquery |

Source : postgresql.org/docs/17/errcodes-appendix.html.

## Style Guidance

ALWAYS write `LATERAL` explicitly when the inner item cross-references outer columns, even when optional (function call form). It signals correlation to readers and tooling.

ALWAYS write an explicit `INNER JOIN LATERAL ... ON true` or `LEFT JOIN LATERAL ... ON true` rather than the comma-join shorthand when a query mixes regular joins with LATERAL : the JOIN tree is easier to read.

ALWAYS add a deterministic `ORDER BY` before `LIMIT N` inside the LATERAL subquery : without it, the choice of inner row is implementation-defined.

NEVER use LATERAL when the inner subquery does NOT reference any outer column : it is a no-op and obscures intent.

NEVER mix `, LATERAL` and `JOIN LATERAL` in the same query : pick one form for readability.
