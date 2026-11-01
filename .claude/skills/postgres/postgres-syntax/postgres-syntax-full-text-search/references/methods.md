# Methods : postgres-syntax-full-text-search

Full API surface for PostgreSQL full-text search. All signatures verified against
PostgreSQL 17 documentation. Every function and type listed is available unchanged
in PostgreSQL 15, 16, and 17.

## Data Types

### tsvector

A sorted list of distinct lexemes with optional positions and weights. Lexemes are
normalized words ; positions enable proximity ranking ; weights (A-D) enable
field-priority ranking.

```sql
SELECT to_tsvector('english', 'a fat cat sat on a mat - it ate a fat rats');
-- 'ate':9 'cat':3 'fat':2,11 'mat':7 'rat':12 'sat':4
```

Notes : stop words (`a`, `on`, `it`) are removed, `rats` is stemmed to `rat`,
punctuation is dropped, positions count every original token including stop words.

### tsquery

A search expression : lexemes combined with boolean and phrase operators. Build
one with a constructor function (preferred) or by casting a literal.

```sql
SELECT 'fat & cat & !dog'::tsquery;     -- explicit cast
SELECT to_tsquery('english', 'fat & cats');   -- 'fat' & 'cat'
```

## The Match Operator

| Form | Equivalent to |
|------|---------------|
| `tsvector @@ tsquery` | direct match |
| `tsquery @@ tsvector` | direct match (operands swappable) |
| `text @@ tsquery` | `to_tsvector(text) @@ tsquery` |
| `text @@ text` | `to_tsvector(text) @@ plainto_tsquery(text)` |

ALWAYS use the explicit `to_tsvector(...) @@ constructor(...)` form in real queries.
The `text @@` shortcuts use the 1-argument `to_tsvector` and cannot use an
expression index.

## Document Parsing

### to_tsvector

```
to_tsvector([ config regconfig, ] document text) returns tsvector
to_tsvector([ config regconfig, ] document json|jsonb) returns tsvector
```

Parses a document into tokens, reduces tokens to lexemes via the config's
dictionaries, drops stop words, returns a `tsvector` with positions.

ALWAYS pass the 2-argument form with an explicit config in index expressions and
in queries against an expression index. The 1-argument form depends on the session
GUC `default_text_search_config` and is not immutable.

### setweight

```
setweight(vector tsvector, weight "char") returns tsvector
setweight(vector tsvector, weight "char", lexemes text[]) returns tsvector
```

Labels every lexeme (or only the listed `lexemes`) of `vector` with `weight`, one
of `A`, `B`, `C`, `D`. Used to mark which document section a lexeme came from so
`ts_rank` can score them differently.

```sql
setweight(to_tsvector('english', title), 'A') ||
setweight(to_tsvector('english', body),  'D')
```

## Query Parsing

### to_tsquery

```
to_tsquery([ config regconfig, ] querytext text) returns tsquery
```

Parses operator syntax : `&` (AND), `|` (OR), `!` (NOT), `<->` (FOLLOWED BY),
`<N>` (distance N), parentheses for grouping, `:*` for prefix match, `:A`/`:B`/
`:C`/`:D` for weight restriction. RAISES SQLSTATE `42601` on malformed input.
NEVER call on raw end-user text.

```sql
SELECT to_tsquery('english', 'The & Fat & Rats');   -- 'fat' & 'rat'
SELECT to_tsquery('english', 'Fat | Rats:AB');       -- 'fat' | 'rat':AB
SELECT to_tsquery('supern:*');                       -- 'supern':*  (prefix)
```

### plainto_tsquery

```
plainto_tsquery([ config regconfig, ] querytext text) returns tsquery
```

Normalizes plain text and joins all surviving lexemes with `&`. Ignores operator
characters, weights, and prefix labels. Never raises an error.

```sql
SELECT plainto_tsquery('english', 'The Fat Rats');   -- 'fat' & 'rat'
```

### phraseto_tsquery

```
phraseto_tsquery([ config regconfig, ] querytext text) returns tsquery
```

Like `plainto_tsquery` but joins lexemes with `<->` (FOLLOWED BY) so they must
appear adjacent and in order. Stop-word gaps become `<N>` operators. Never raises
an error.

```sql
SELECT phraseto_tsquery('english', 'The Fat Rats');        -- 'fat' <-> 'rat'
SELECT phraseto_tsquery('english', 'the cats ate the rats');
-- 'cat' <-> 'ate' <2> 'rat'
```

### websearch_to_tsquery

```
websearch_to_tsquery([ config regconfig, ] querytext text) returns tsquery
```

Parses a Google-like syntax. NEVER raises an error : the only constructor safe for
untrusted input. Supports :

| Input syntax | Result |
|--------------|--------|
| unquoted words | joined with `&` |
| `"quoted phrase"` | joined with `<->` |
| the word `or` | `\|` operator |
| leading `-` | `!` operator |

```sql
SELECT websearch_to_tsquery('english', 'fat rats');             -- 'fat' & 'rat'
SELECT websearch_to_tsquery('english', '"supernovae stars" -crab');
-- 'supernova' <-> 'star' & !'crab'
SELECT websearch_to_tsquery('english', '"sad cat" or "fat rat"');
-- 'sad' <-> 'cat' | 'fat' <-> 'rat'
```

## tsquery Operators

| Operator | Name | Meaning |
|----------|------|---------|
| `&` | AND | both lexemes must match |
| `\|` | OR | at least one lexeme must match |
| `!` | NOT | lexeme must not match |
| `<->` | FOLLOWED BY | lexemes adjacent and in order |
| `<N>` | distance N | lexemes N positions apart, in order (`<1>` = `<->`) |
| `:*` | prefix | matches any lexeme with this prefix |
| `:A` `:B` `:C` `:D` | weight restriction | match only lexemes with these weights |

## Ranking

### ts_rank

```
ts_rank([ weights float4[], ] vector tsvector, query tsquery
        [, normalization integer ]) returns float4
```

Ranks by frequency of matching lexemes. `weights` is the `{D, C, B, A}` array,
default `{0.1, 0.2, 0.4, 1.0}`.

### ts_rank_cd

```
ts_rank_cd([ weights float4[], ] vector tsvector, query tsquery
           [, normalization integer ]) returns float4
```

Cover-density ranking : also accounts for how close matching lexemes are. Requires
positional information ; never use it on a `tsvector` whose positions were stripped.

### Normalization flags (bit mask, combine with `|`)

| Flag | Effect |
|------|--------|
| `0` | ignore document length (default) |
| `1` | divide by `1 + log(doc length)` |
| `2` | divide by document length |
| `4` | divide by mean harmonic distance between extents (`ts_rank_cd` only) |
| `8` | divide by number of unique words |
| `16` | divide by `1 + log(unique words)` |
| `32` | divide by `rank / (rank + 1)`, scaling output into 0-1 |

```sql
SELECT title, ts_rank_cd(search, q, 32) AS rank
FROM docs, websearch_to_tsquery('english', $1) AS q
WHERE search @@ q ORDER BY rank DESC LIMIT 10;
```

## Highlighting

### ts_headline

```
ts_headline([ config regconfig, ] document text,   query tsquery [, options text ]) returns text
ts_headline([ config regconfig, ] document json|jsonb, query tsquery [, options text ]) returns text
```

Returns an excerpt of `document` with matched lexemes wrapped in delimiters. Works
on raw text, NOT on the indexed `tsvector` : not index-accelerated, so run it only
on a filtered, `LIMIT`-ed result set. Output is NOT XSS-safe.

| Option | Type | Default | Meaning |
|--------|------|---------|---------|
| `MaxWords` | integer | 35 | longest headline length |
| `MinWords` | integer | 15 | shortest headline length |
| `ShortWord` | integer | 3 | drop words this short at the start/end |
| `HighlightAll` | boolean | false | use the whole document |
| `MaxFragments` | integer | 0 | max fragments ; 0 = single-window mode |
| `StartSel` | string | `<b>` | highlight start delimiter |
| `StopSel` | string | `</b>` | highlight stop delimiter |
| `FragmentDelimiter` | string | ` ... ` | separator between fragments |

## Index Types

### GIN (preferred)

```sql
CREATE INDEX name ON table USING gin (tsvector_column);
CREATE INDEX name ON table USING gin (to_tsvector('english', body));
```

Inverted index, one entry per lexeme. Lossless : no false matches. Fastest lookup.
Larger and slower to update than GiST ; raise `maintenance_work_mem` to speed the
build. The recommended FTS index type.

### GiST

```sql
CREATE INDEX name ON table USING gist (tsvector_column);
CREATE INDEX name ON table USING gist (tsvector_column tsvector_ops(siglen = 124));
```

Signature-tree index. Lossy : can produce false matches that PostgreSQL rechecks
against the heap row. Smaller, cheaper to update. `siglen` (default 124 bytes, max
2024) trades index size for fewer false matches. Use only for small, very
frequently updated tables, or when indexing `tsquery` values.

## Generated tsvector Column (v12+)

```sql
ALTER TABLE docs ADD COLUMN search tsvector
  GENERATED ALWAYS AS (
    to_tsvector('english', coalesce(title,'') || ' ' || coalesce(body,''))
  ) STORED;
CREATE INDEX ON docs USING gin (search);
```

Auto-maintained by PostgreSQL on every INSERT/UPDATE of the source columns. The
preferred modern alternative to a trigger-maintained column. Always wrap nullable
source columns in `coalesce(col, '')`.

## Configurations and Dictionaries

A text search configuration combines a parser with an ordered set of dictionaries.
Built-in configs include `simple` (lowercase only, no stemming, keeps stop words)
and language configs such as `english` (stems, drops English stop words). The
config controls how BOTH `to_tsvector` and the query constructors normalize text :
the index config and the query config MUST be identical for the index to be used.

Inspect available configs with `\dF` in psql or
`SELECT cfgname FROM pg_ts_config;`.

## Related : pg_trgm (different tool)

`pg_trgm` does character-trigram matching for typos, substrings, and `LIKE`
acceleration. It is NOT full-text search. Key surface :

| Item | Meaning |
|------|---------|
| `similarity(a, b)` | 0-1 trigram similarity score |
| `a % b` | true when similarity exceeds the threshold (default 0.3) |
| `a <-> b` | distance, `1 - similarity` |
| `gin_trgm_ops` / `gist_trgm_ops` | opclasses to accelerate `LIKE`, `ILIKE`, `~`, `%` |

```sql
CREATE EXTENSION pg_trgm;
CREATE INDEX ON t USING gin (name gin_trgm_ops);
SELECT name FROM t WHERE name % 'jonh smith' ORDER BY similarity(name,'jonh smith') DESC;
```

## Sources

- https://www.postgresql.org/docs/17/textsearch-intro.html
- https://www.postgresql.org/docs/17/textsearch-tables.html
- https://www.postgresql.org/docs/17/textsearch-controls.html
- https://www.postgresql.org/docs/17/textsearch-indexes.html
- https://www.postgresql.org/docs/17/textsearch.html
- https://www.postgresql.org/docs/17/pgtrgm.html
