# postgres-impl-pgvector-similarity : Methods Reference

API surface for pgvector. Verified against github.com/pgvector/pgvector (Last Verified : 2026-05-19). Targets pgvector 0.7+ on PostgreSQL 15, 16, 17.

## Installation

```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

Adds the `vector`, `halfvec`, `sparsevec`, and `bit`-based vector operators, the `hnsw` and `ivfflat` index access methods, and all operator classes. pgvector supports PostgreSQL 13+.

## Vector types

| Type | Element | Max dims (storage) | Max dims (indexable) | Use for |
|---|---|---|---|---|
| `vector(n)` | float32 (4 bytes) | 16000 | 2000 | standard dense embeddings |
| `halfvec(n)` | float16 (2 bytes) | 16000 | 4000 | high-dim embeddings, half the storage |
| `sparsevec(n)` | sparse float | 16000 non-zero | 1000 non-zero | mostly-zero high-dim vectors |
| `bit(n)` | bit | 64000 | 64000 | binary / hashed embeddings |

`halfvec` added in pgvector 0.7.0. `sparsevec` added in 0.7.0.

## Distance operators

| Operator | Metric | Valid on | Notes |
|---|---|---|---|
| `<->` | L2 / Euclidean distance | vector, halfvec, sparsevec | smaller = closer |
| `<=>` | cosine distance | vector, halfvec, sparsevec | range `[0, 2]` ; smaller = closer |
| `<#>` | negative inner product | vector, halfvec, sparsevec | returns `-(inner product)` ; multiply by `-1` for a score |
| `<+>` | L1 / taxicab distance | vector, halfvec, sparsevec | added in pgvector 0.7.0 |
| `<~>` | Hamming distance | bit | added in 0.7.0 |
| `<%>` | Jaccard distance | bit | added in 0.7.0 |

`<#>` is negative because PostgreSQL index scans only return rows in ascending order ; ordering ascending by the negative inner product puts the most-similar rows first.

## Index methods

### HNSW

```sql
CREATE INDEX ON table USING hnsw (column opclass) WITH (m = 16, ef_construction = 64);
```

| Build parameter | Default | Meaning |
|---|---|---|
| `m` | 16 | max connections per graph layer ; higher = better recall, larger index |
| `ef_construction` | 64 | build-time candidate-list size ; higher = better recall, slower build |

- No training step. Can be built on an empty table.
- Best recall and query speed of the two methods.
- Higher build time and memory than ivfflat.
- HNSW index support added in pgvector 0.5.0.

### ivfflat

```sql
CREATE INDEX ON table USING ivfflat (column opclass) WITH (lists = 100);
```

| Build parameter | Recommendation | Meaning |
|---|---|---|
| `lists` | `rows / 1000` for <= 1M rows ; `sqrt(rows)` for > 1M rows | number of k-means cluster centroids |

- MUST be created after representative rows are loaded , `lists` centroids train on the rows present at build time.
- Faster to build, lower memory than HNSW, but lower recall.
- Rebuild after a large data change so centroids re-train.

## Operator classes

The index operator class MUST match the distance operator used by the query.

| Distance operator | HNSW opclass (vector) | ivfflat opclass (vector) |
|---|---|---|
| `<->` L2 | `vector_l2_ops` | `vector_l2_ops` |
| `<=>` cosine | `vector_cosine_ops` | `vector_cosine_ops` |
| `<#>` inner product | `vector_ip_ops` | `vector_ip_ops` |
| `<+>` L1 | `vector_l1_ops` | `vector_l1_ops` |

`halfvec` columns use `halfvec_l2_ops`, `halfvec_cosine_ops`, `halfvec_ip_ops`, `halfvec_l1_ops`. `sparsevec` columns use `sparsevec_l2_ops`, `sparsevec_cosine_ops`, `sparsevec_ip_ops`, `sparsevec_l1_ops`. `bit` columns use `bit_hamming_ops` and `bit_jaccard_ops`.

## Query-time settings

| Setting | Index | Default | Effect |
|---|---|---|---|
| `hnsw.ef_search` | HNSW | 40 | search candidate-list size ; higher = better recall, slower query |
| `ivfflat.probes` | ivfflat | 1 | number of clusters probed ; higher = better recall, slower query |
| `hnsw.iterative_scan` | HNSW | `off` | `strict_order` / `relaxed_order` ; re-probe until `LIMIT` met (v0.8+) |
| `ivfflat.iterative_scan` | ivfflat | `off` | `relaxed_order` ; re-probe until `LIMIT` met (v0.8+) |

These are session-level. Use `SET LOCAL` inside a transaction to scope to one statement group. None require an index rebuild.

## Query pattern

```sql
SELECT id, embedding <=> $1 AS distance
FROM table
ORDER BY embedding <=> $1
LIMIT k;
```

The index accelerates the `ORDER BY <distance> LIMIT k` shape only , the k-nearest-neighbour pattern. A bare `WHERE <distance> < threshold` without `ORDER BY ... LIMIT` does not use the index.

## Dimension-limit rules

- `vector` : index up to 2000 dimensions. Above 2000, cast to `halfvec` and index the cast expression.
- `halfvec` : index up to 4000 dimensions.
- The query must repeat the exact cast expression used in the index, or the index is not matched.
- Changing a column's dimension count invalidates an existing index , drop and rebuild.

## Version history

| Feature | pgvector version |
|---|---|
| `ivfflat` index | 0.1.0 |
| HNSW index | 0.5.0 |
| `halfvec`, `sparsevec` types | 0.7.0 |
| `<+>` L1 operator, `<~>` Hamming, `<%>` Jaccard | 0.7.0 |
| Iterative index scans | 0.8.0 |

## Source URLs

- https://github.com/pgvector/pgvector
