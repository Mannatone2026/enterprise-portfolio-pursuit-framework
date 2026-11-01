# Methods : Arrays, Ranges, Multiranges

Full API surface. Verified against PostgreSQL 17 official documentation
(arrays.html, rangetypes.html, functions-array.html, functions-range.html).
All listed surfaces exist in PostgreSQL 15, 16, and 17 unless a version note says otherwise.

## Array Type Declaration

| Syntax | Meaning |
|--------|---------|
| `integer[]` | One-dimensional array of integer |
| `text[][]` | Two-dimensional array of text |
| `integer ARRAY` | SQL-standard equivalent of `integer[]` |
| `integer[4]` | Size `4` is documentation only ; NOT enforced at runtime |

ALWAYS : append `[]` for any dimension count. PostgreSQL ignores both the
declared size and the declared dimension count : `integer[]` and `integer[4][4]`
behave identically. Actual dimensions are set by the stored value.

## Array Literals

| Form | Example | Notes |
|------|---------|-------|
| `ARRAY[...]` constructor | `ARRAY[1,2,3]` | Preferred ; elements are expressions |
| String literal | `'{1,2,3}'::int[]` | Must be cast ; whole literal in single quotes |
| 2D constructor | `ARRAY[[1,2],[3,4]]` | Sub-arrays must match dimensions |
| 2D string literal | `'{{1,2},{3,4}}'::int[]` | Curly braces nest |
| Empty array | `'{}'::int[]` or `ARRAY[]::int[]` | `ARRAY[]` needs an explicit cast |

Quoting inside a string literal : quote any element containing a comma, curly
brace, double quote, backslash, or whitespace, plus empty strings and the
literal text `NULL`. Unquoted `NULL` (any case) means a NULL element.

## Array Element Access

| Syntax | Result |
|--------|--------|
| `arr[1]` | First element (subscripts are 1-based by default) |
| `arr[2:4]` | Slice : elements 2 through 4 |
| `arr[:3]` | Slice from lower bound through 3 |
| `arr[2:]` | Slice from 2 through upper bound |
| `arr[99]` (out of bounds) | Returns `NULL` ; does NOT raise an error |
| `arr[2:99]` (slice past end) | Returns the available sub-array, never NULL |

Out-of-range single-element access returns `NULL`. SQLSTATE `2202E`
(`array_subscript_error`) is raised only for an invalid slice expression or
when assigning past the end of a multidimensional array.

## Array Operators

| Operator | Signature | Description |
|----------|-----------|-------------|
| `=` `<>` | `anyarray <op> anyarray` | Element-wise equality / inequality |
| `<` `<=` `>` `>=` | `anyarray <op> anyarray` | Lexicographic ordering |
| `@>` | `anyarray @> anyarray -> boolean` | Left array contains right |
| `<@` | `anyarray <@ anyarray -> boolean` | Left array is contained by right |
| `&&` | `anyarray && anyarray -> boolean` | Arrays share at least one element |
| `\|\|` | `anyarray \|\| anyarray -> anyarray` | Concatenate two arrays |
| `\|\|` | `anycompatible \|\| anyarray` | Prepend element |
| `\|\|` | `anyarray \|\| anycompatible` | Append element |

`= ANY(arr)` and `= ALL(arr)` are not operators on the array : they are scalar
quantified comparisons. `x = ANY(arr)` is true when `x` equals any element ;
`x = ALL(arr)` is true when `x` equals every element. Neither uses a GIN index
on a column array. `@>`, `<@`, `&&` ARE GIN-indexable via the `array_ops`
opclass.

## Array Functions

| Function | Signature | Example output |
|----------|-----------|----------------|
| `array_append` | `(anyarray, anyelement) -> anyarray` | `array_append(ARRAY[1,2],3)` -> `{1,2,3}` |
| `array_prepend` | `(anyelement, anyarray) -> anyarray` | `array_prepend(1,ARRAY[2,3])` -> `{1,2,3}` |
| `array_cat` | `(anyarray, anyarray) -> anyarray` | `array_cat(ARRAY[1,2],ARRAY[3])` -> `{1,2,3}` |
| `array_remove` | `(anyarray, anyelement) -> anyarray` | `array_remove(ARRAY[1,2,2],2)` -> `{1}` (1-D only) |
| `array_replace` | `(anyarray, anyelement, anyelement) -> anyarray` | `array_replace(ARRAY[1,5,4],5,3)` -> `{1,3,4}` |
| `array_position` | `(anyarray, anyelement [,int]) -> integer` | `array_position(ARRAY['a','b'],'b')` -> `2` (1-D only) |
| `array_positions` | `(anyarray, anyelement) -> integer[]` | `array_positions(ARRAY['a','a'],'a')` -> `{1,2}` |
| `array_length` | `(anyarray, int) -> integer` | `array_length(ARRAY[1,2,3],1)` -> `3` |
| `array_lower` | `(anyarray, int) -> integer` | Lower bound of a dimension |
| `array_upper` | `(anyarray, int) -> integer` | Upper bound of a dimension |
| `array_ndims` | `(anyarray) -> integer` | Number of dimensions |
| `array_dims` | `(anyarray) -> text` | `array_dims(ARRAY[[1,2],[3,4]])` -> `[1:2][1:2]` |
| `cardinality` | `(anyarray) -> integer` | Total element count across all dimensions |
| `array_to_string` | `(anyarray, text [,text]) -> text` | Join with delimiter ; 3rd arg is NULL placeholder |
| `string_to_array` | `(text, text [,text]) -> text[]` | Split a string into a text array |
| `trim_array` | `(anyarray, int) -> anyarray` | Remove last n elements |
| `unnest` | `(anyarray) -> setof anyelement` | Expand array into one row per element |
| `unnest` | `(anyarray, anyarray, ...)` | Expand several arrays side by side, shorter padded with NULL |
| `generate_subscripts` | `(anyarray, int) -> setof integer` | Emit valid subscripts of a dimension |
| `array_agg` | `(anyelement) -> anyarray` (aggregate) | Collapse a column of rows into an array |

Version notes :
- `array_sample(anyarray, int) -> anyarray` and `array_shuffle(anyarray) -> anyarray` : **v16+**.
- `array_length`, `cardinality`, `array_position` : available in all supported versions.

### unnest WITH ORDINALITY

`unnest` is a set-returning function ; in a `FROM` clause it can carry an
ordinal column :

```sql
SELECT value, n
FROM unnest(ARRAY['a','b','c']) WITH ORDINALITY AS t(value, n);
```

`WITH ORDINALITY` appends a `bigint` column counting `1, 2, 3, ...`. The column
alias list `t(value, n)` names both the element column and the ordinal column.

## Range Types

| Range type | Subtype | Discrete? |
|------------|---------|-----------|
| `int4range` | `integer` | discrete (canonical `[)`) |
| `int8range` | `bigint` | discrete (canonical `[)`) |
| `numrange` | `numeric` | continuous |
| `tsrange` | `timestamp` | continuous |
| `tstzrange` | `timestamptz` | continuous |
| `daterange` | `date` | discrete (canonical `[)`) |

Each range type has a matching multirange type : `int4multirange`,
`int8multirange`, `nummultirange`, `tsmultirange`, `tstzmultirange`,
`datemultirange`. Multirange types exist since **v14**, so they are present in
every supported version.

## Range Literal Input

| Literal | Meaning |
|---------|---------|
| `'[3,7)'` | 3 inclusive, 7 exclusive |
| `'(3,7]'` | 3 exclusive, 7 inclusive |
| `'[3,7]'` | both inclusive |
| `'(3,7)'` | both exclusive |
| `'empty'` | the empty range |
| `'(,5]'` | unbounded below, 5 inclusive |
| `'[5,)'` | 5 inclusive, unbounded above |
| `'(,)'` | full range, both bounds infinite |

`[)` is the canonical form for discrete types : `'[4,8]'::int4range` is stored
and printed as `[4,9)`. `'[4,4)'::int4range` normalises to `empty`.

## Range Constructor Functions

| Form | Behaviour |
|------|-----------|
| `int4range(lo, hi)` | Two-arg : always `[)` bounds |
| `numrange(lo, hi, '[]')` | Three-arg : 3rd arg sets bound inclusivity |
| `tstzrange(lo, NULL)` | `NULL` argument means an unbounded side |

## Range and Multirange Operators

| Operator | Description |
|----------|-------------|
| `@>` | Contains a range, multirange, or element |
| `<@` | Is contained by |
| `&&` | Overlaps : the two share at least one point |
| `<<` | Strictly left of (no overlap, fully below) |
| `>>` | Strictly right of |
| `&<` | Does not extend to the right of |
| `&>` | Does not extend to the left of |
| `-\|-` | Adjacent : touch but do not overlap |
| `+` | Union (only when result is a single range) |
| `*` | Intersection |
| `-` | Difference |

`@>`, `<@`, `&&`, `<<`, `>>`, `&<`, `&>`, `-|-` are GiST- and SP-GiST-indexable.
`=` on a range uses the default btree index.

## Range and Multirange Functions

| Function | Returns | Description |
|----------|---------|-------------|
| `lower(anyrange)` | element | Lower bound (NULL if unbounded) |
| `upper(anyrange)` | element | Upper bound (NULL if unbounded) |
| `isempty(anyrange)` | boolean | True for the empty range |
| `lower_inc(anyrange)` | boolean | Lower bound inclusive? |
| `upper_inc(anyrange)` | boolean | Upper bound inclusive? |
| `lower_inf(anyrange)` | boolean | Lower bound infinite? |
| `upper_inf(anyrange)` | boolean | Upper bound infinite? |
| `range_merge(anyrange, anyrange)` | range | Smallest range covering both inputs |
| `range_merge(anymultirange)` | range | Smallest range covering the whole multirange |
| `multirange(anyrange)` | multirange | Wrap one range as a multirange |
| `unnest(anymultirange)` | setof anyrange | Expand multirange into its component ranges |

## Range Aggregate Functions

| Aggregate | Signature | Description |
|-----------|-----------|-------------|
| `range_agg` | `range_agg(anyrange) -> anymultirange` | Union of all input ranges |
| `range_agg` | `range_agg(anymultirange) -> anymultirange` | Union of all input multiranges |
| `range_intersect_agg` | `range_intersect_agg(anyrange) -> anymultirange` | Intersection of all inputs |
| `range_intersect_agg` | `range_intersect_agg(anymultirange) -> anymultirange` | Intersection of multiranges |

`range_agg` and `range_intersect_agg` were introduced in **v14** ; both are
available in every supported version (15, 16, 17).

## Indexing Summary

| Column type | Query operators | Index access method |
|-------------|-----------------|---------------------|
| Array | `@>`, `<@`, `&&` | `GIN` (opclass `array_ops`) |
| Array | `= ANY`, `= ALL` | none (scalar scan per row) |
| Range | `@>`, `<@`, `&&`, `-\|-`, `<<`, `>>` | `GIST` or `SP-GIST` |
| Range | `=` only | `btree` (default) |
| Range (non-overlap rule) | constraint | `EXCLUDE USING gist` builds GiST automatically |

## Exclusion Constraint Syntax

```sql
EXCLUDE USING gist (col_a WITH operator_a, col_b WITH operator_b)
```

The constraint forbids any two rows where every listed `colN operator_N colN`
comparison is true simultaneously. For non-overlap rules, the operator for the
range column is `&&`. To scope the rule by an equality column (room, vehicle),
add `col WITH =` ; the `=` operator on non-GiST types (`text`, `integer`)
requires `CREATE EXTENSION btree_gist`.
