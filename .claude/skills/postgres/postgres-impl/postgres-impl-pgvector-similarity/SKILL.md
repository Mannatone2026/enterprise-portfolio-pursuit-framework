---
name: postgres-impl-pgvector-similarity
description: >
  Use when storing embeddings for semantic search, picking hnsw vs ivfflat, or debugging why a vector query does a seqscan.
  Prevents index opclass not matching the query distance operator (index unused), building ivfflat on an empty table (poor recall), and exceeding the indexable dimension limit.
  Covers CREATE EXTENSION vector, vector / halfvec / sparsevec / bit types, distance operators (<->, <=>, <#>, <+>), ivfflat vs hnsw decision, operator classes, dimension limits, ORDER BY ... LIMIT k query pattern, ef_search tuning, hybrid search with tsvector.
  Keywords: pgvector, vector, embedding, hnsw, ivfflat, cosine distance, <=>, <->, similarity search, semantic search, vector_cosine_ops, dimension limit, hybrid search, vector query is slow, embedding seqscan, which vector index, RAG retrieval
license: MIT
compatibility: "Designed for Claude Code. Requires PostgreSQL 15, 16, or 17 with pgvector 0.7+."
metadata:
  author: OpenAEC-Foundation
  version: "1.0"
---

# postgres-impl-pgvector-similarity

## Quick Reference :

pgvector adds vector similarity search to PostgreSQL while keeping ACID, transactions, and standard SQL. Install with `CREATE EXTENSION vector`. Store embeddings in a `vector(n)` column, query with `ORDER BY embedding <=> $1 LIMIT k`.

Four storage types : `vector` (float32, store up to 16000 dim, index up to 2000), `halfvec` (float16, store up to 16000, index up to **4000** , use it to index high-dimension embeddings), `sparsevec` (sparse, store up to 16000 non-zero), `bit` (binary). Six distance operators : `<->` L2, `<=>` cosine, `<#>` **negative** inner product, `<+>` L1 (v0.7+), `<~>` Hamming and `<%>` Jaccard (bit only).

Two index methods, both **approximate** (ANN) : **HNSW** is the default choice , no training step, best recall and query speed, slower build and more memory. **ivfflat** is faster to build and lighter, but recall is lower AND it MUST be built after representative rows are loaded (it k-means-trains on existing data).

The single most common failure : the index operator class MUST match the distance operator the query uses. An `hnsw (embedding vector_cosine_ops)` index accelerates `<=>` only , a query using `<->` ignores it and does a Seq Scan. ALWAYS pick one distance metric per column, build the matching opclass, and query with the matching operator.

## When To Use This Skill :

ALWAYS use this skill when :
- Storing embeddings for semantic search, RAG retrieval, recommendation, or deduplication
- Choosing between HNSW and ivfflat, or between `vector` and `halfvec`
- A vector query is slow or `EXPLAIN` shows a Seq Scan on an embedding column
- Tuning recall versus speed (`hnsw.ef_search`, `ivfflat.probes`)
- Combining vector similarity with keyword / full-text search (hybrid search)

NEVER use this skill for :
- General B-tree / GIN / GiST index selection : see `postgres-impl-indexing-strategy`
- `tsvector` full-text-search mechanics on their own : see the full-text-search syntax skill
- Reading `EXPLAIN` output in general : see `postgres-impl-explain-analyze`

## Decision Trees :

### Which vector type? :

```
Embedding dimensionality and density?
├── <= 2000 dims, dense float embeddings :
│       vector(n)        , index directly with hnsw or ivfflat
├── 2001 - 4000 dims, dense, OR want ~2x less memory and storage :
│       halfvec(n)       , store as vector, index as halfvec via a cast
│       OR define the column as halfvec(n) directly
├── high-dimensional but mostly zero (BM25-style sparse vectors) :
│       sparsevec(n)
└── binary / hashed embeddings :
        bit(n)           , query with <~> Hamming or <%> Jaccard
```

### HNSW or ivfflat? :

```
Default to HNSW. Choose ivfflat only for a specific reason.
├── Need best recall + query speed, can afford build time + memory :
│       HNSW    , no training, build anytime (even on an empty table)
├── Index build time / memory is the hard constraint, recall can be lower :
│       ivfflat , MUST build AFTER representative rows are loaded
└── Table is still empty / tiny and will grow :
        HNSW now, OR defer ivfflat until data is loaded.
        NEVER build ivfflat on an empty table , centroids are meaningless.
```

### Distance operator and matching operator class :

```
Which similarity metric does the application use?
├── Cosine similarity (most text-embedding models, e.g. OpenAI) :
│       operator <=>   opclass vector_cosine_ops
├── Euclidean / L2 distance :
│       operator <->   opclass vector_l2_ops
├── Inner product (ONLY meaningful on unit-normalized vectors) :
│       operator <#>   opclass vector_ip_ops    , note <#> is NEGATIVE
├── L1 / Manhattan distance (v0.7+) :
│       operator <+>   opclass vector_l1_ops
└── Binary vectors :
        operator <~> Hamming / <%> Jaccard   opclass bit_hamming_ops / bit_jaccard_ops

RULE : index opclass and query operator MUST be the same metric,
       or the index is never used.
```

## Patterns :

### Pattern 1 : Install the extension and create the embedding column

ALWAYS declare the column with a fixed dimension `vector(n)` so every row is validated.
NEVER store mixed-dimension vectors in one column , distance operators error on a dimension mismatch.

```sql
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE documents (
    id        bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    content   text NOT NULL,
    embedding vector(1536)            -- e.g. OpenAI text-embedding-3-small
);
```

WHY : `vector(1536)` pins the dimension. An `INSERT` of a different-length array is rejected at write time, not silently at query time. The extension also adds `halfvec`, `sparsevec`, and `bit` plus their operators and operator classes. Source : github.com/pgvector/pgvector.

### Pattern 2 : HNSW index , the default choice

ALWAYS build HNSW with the operator class that matches the query's distance operator.
NEVER expect one HNSW index to serve two different distance metrics , build one index per metric.

```sql
-- Cosine-distance index (matches the <=> query operator)
CREATE INDEX idx_documents_embedding_hnsw
    ON documents
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
```

WHY : HNSW (Hierarchical Navigable Small Worlds) is a graph index , it needs no training data and can be built on an empty table. `m` (default 16) is the max connections per graph layer ; `ef_construction` (default 64) is the build-time candidate-list size. Higher values raise recall at the cost of build time and index size. HNSW gives the best recall and query speed of the two methods. Source : github.com/pgvector/pgvector.

### Pattern 3 : ivfflat index , build AFTER loading data

ALWAYS load representative rows before creating an ivfflat index.
NEVER create an ivfflat index on an empty or tiny table , its k-means centroids train on the rows present at build time.

```sql
-- 1. Load the embeddings FIRST, then create the index.
-- lists : rows/1000 for <= 1M rows ; sqrt(rows) for > 1M rows
CREATE INDEX idx_documents_embedding_ivf
    ON documents
    USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 100);
```

WHY : ivfflat partitions vectors into `lists` clusters around k-means centroids ; a query probes the nearest clusters. If the index is built before data exists, the centroids are not representative and recall collapses. ivfflat builds faster and uses less memory than HNSW but has lower recall. After a large data change, drop and rebuild so the centroids re-train. Source : github.com/pgvector/pgvector.

### Pattern 4 : The similarity query , ORDER BY distance LIMIT k

ALWAYS write `ORDER BY embedding <op> $1 LIMIT k` so the planner can use the index.
NEVER put the distance in a `WHERE` threshold without an `ORDER BY ... LIMIT` , a bare `WHERE embedding <=> $1 < 0.3` cannot use the ANN index and does a Seq Scan.

```sql
-- k nearest neighbours by cosine distance
SELECT id, content, embedding <=> $1 AS distance
FROM documents
ORDER BY embedding <=> $1
LIMIT 10;

-- Inner product : <#> is NEGATIVE, negate it to read a real score
SELECT id, (embedding <#> $1) * -1 AS inner_product
FROM documents
ORDER BY embedding <#> $1
LIMIT 10;
```

WHY : pgvector indexes accelerate the `ORDER BY <distance> LIMIT k` shape only , this is the k-nearest-neighbour pattern. `<#>` returns the **negative** inner product, because the index can only return ascending-ordered results ; ordering ascending by a negative value puts the most-similar rows first. Confirm the index is used with `EXPLAIN` , look for `Index Scan using ...hnsw`, not `Seq Scan`. Source : github.com/pgvector/pgvector.

### Pattern 5 : Tune recall versus speed at query time

ALWAYS raise `hnsw.ef_search` (or `ivfflat.probes`) when recall is too low ; both are session-level runtime knobs.
NEVER rebuild the index to change recall , the runtime parameters do it without a rebuild.

```sql
-- HNSW : default ef_search is 40 ; raise for higher recall
SET hnsw.ef_search = 100;

-- ivfflat : default probes is 1 ; raise for higher recall
SET ivfflat.probes = 10;

-- Scope to one transaction instead of the whole session
BEGIN;
SET LOCAL hnsw.ef_search = 200;
SELECT id FROM documents ORDER BY embedding <=> $1 LIMIT 10;
COMMIT;
```

WHY : both index methods are approximate. `hnsw.ef_search` widens the search candidate list ; `ivfflat.probes` searches more clusters. Higher values increase recall and latency linearly , tune to the recall target. These are query-time settings ; the index itself is unchanged. Source : github.com/pgvector/pgvector.

### Pattern 6 : Filtered similarity search

ALWAYS use a partial index per filter value, or iterative scans (v0.8+), when combining a `WHERE` filter with vector search.
NEVER assume `WHERE category = $2 ORDER BY embedding <=> $1 LIMIT 10` returns 10 rows , the ANN index filters AFTER the nearest-neighbour search and may return fewer.

```sql
-- Option A : partial HNSW index per frequently-filtered value
CREATE INDEX idx_docs_emb_published
    ON documents USING hnsw (embedding vector_cosine_ops)
    WHERE status = 'published';

-- Option B (pgvector v0.8+) : iterative scans re-probe until LIMIT is met
SET hnsw.iterative_scan = strict_order;
SELECT id FROM documents
WHERE category = $2
ORDER BY embedding <=> $1
LIMIT 10;
```

WHY : an ANN index returns its best candidates first, then the `WHERE` filter discards non-matching rows , so a selective filter can leave fewer than `LIMIT` rows. A partial index restricted to the filter predicate keeps the whole index relevant. Iterative scans (v0.8+) automatically re-probe the index until `LIMIT` is satisfied. Source : github.com/pgvector/pgvector.

### Pattern 7 : Hybrid search , vector similarity plus full-text

ALWAYS combine semantic and keyword results with Reciprocal Rank Fusion (RRF) rather than mixing raw distance and `ts_rank` scores.
NEVER add a cosine distance and a `ts_rank` score directly , the two scales are not comparable.

```sql
-- RRF : fuse vector ranking and full-text ranking by RANK, not raw score
WITH semantic AS (
    SELECT id, row_number() OVER (ORDER BY embedding <=> $1) AS rank
    FROM documents ORDER BY embedding <=> $1 LIMIT 50
),
keyword AS (
    SELECT id, row_number() OVER (
               ORDER BY ts_rank(to_tsvector('english', content),
                                plainto_tsquery('english', $2)) DESC) AS rank
    FROM documents
    WHERE to_tsvector('english', content) @@ plainto_tsquery('english', $2)
    LIMIT 50
)
SELECT COALESCE(s.id, k.id) AS id,
       COALESCE(1.0 / (60 + s.rank), 0) + COALESCE(1.0 / (60 + k.rank), 0) AS score
FROM semantic s
FULL OUTER JOIN keyword k USING (id)
ORDER BY score DESC
LIMIT 10;
```

WHY : RRF fuses two result lists by rank position (the constant 60 is the standard damping term), so it is scale-independent , a cosine distance in `[0, 2]` and a `ts_rank` score on a different scale never need to be normalized against each other. Source : github.com/pgvector/pgvector ; postgresql.org/docs/17/textsearch-controls.html.

### Pattern 8 : halfvec to index high-dimension embeddings

ALWAYS cast to `halfvec` when the embedding has more than 2000 dimensions , `vector` cannot be indexed beyond 2000.
NEVER leave a 3072-dimension `vector` column unindexed and expect fast search , every query is an exact Seq Scan.

```sql
-- 3072-dim embedding : store as vector, index the halfvec cast
CREATE INDEX idx_documents_embedding_half
    ON documents
    USING hnsw ((embedding::halfvec(3072)) halfvec_cosine_ops);

-- The query must use the SAME cast to match the index
SELECT id FROM documents
ORDER BY embedding::halfvec(3072) <=> $1::halfvec(3072)
LIMIT 10;
```

WHY : `vector` is indexable up to 2000 dimensions ; `halfvec` (float16) raises that to 4000 and halves storage. The index is on the expression `embedding::halfvec(3072)`, so the query MUST repeat the identical cast or the index is not matched. Recall loss from float16 is usually small. Source : github.com/pgvector/pgvector.

## Anti-Patterns :

(One-liners ; full diagnosis + fix in `references/anti-patterns.md`.)

- Index opclass does not match the query operator , `vector_cosine_ops` index, query uses `<->` : index ignored, Seq Scan.
- ivfflat index built on an empty / tiny table , centroids untrained, recall collapses. Build after loading data.
- Embedding above the indexable dimension limit (`vector` > 2000) , no index possible. Cast to `halfvec` (up to 4000).
- Inner product `<#>` on non-normalized vectors , ranking is dominated by magnitude, not direction. Use `<=>` cosine or normalize first.
- Reading `<#>` as a positive score , it is the NEGATIVE inner product ; multiply by `-1`.
- `WHERE embedding <=> $1 < 0.3` with no `ORDER BY ... LIMIT` , distance threshold cannot use the ANN index.
- Filtered query expecting exactly `LIMIT` rows , ANN filters after search ; use a partial index or iterative scans (v0.8+).
- One index expected to serve two distance metrics , build one index per metric.
- Mixed-dimension vectors in one column , distance operators error on dimension mismatch. Pin `vector(n)`.
- Changing embedding dimensions without rebuilding the index , the old index is invalid for the new vectors.
- Adding raw cosine distance and `ts_rank` for hybrid search , incomparable scales ; fuse by rank with RRF.

## Reference Links :

- [references/methods.md](references/methods.md) : Vector types, distance operators, index parameters, operator classes, dimension limits, runtime settings
- [references/examples.md](references/examples.md) : Verified, version-annotated examples for install, HNSW, ivfflat, queries, filtered search, hybrid RRF, halfvec
- [references/anti-patterns.md](references/anti-patterns.md) : pgvector anti-patterns with cause, symptom, and fix

## See Also :

- `postgres-impl-indexing-strategy` : general index-method selection ; pgvector indexes are a specialized ANN case
- `postgres-impl-explain-analyze` : confirming a vector query uses the index instead of a Seq Scan
- `postgres-syntax-window-functions` : `row_number()` for the rank used in Reciprocal Rank Fusion
- `postgres-core-version-matrix` : pgvector versioning ; HNSW added in 0.5, halfvec / sparsevec / `<+>` in 0.7, iterative scans in 0.8
- Vooronderzoek section : §11 (pgvector)
- Official docs : https://github.com/pgvector/pgvector
