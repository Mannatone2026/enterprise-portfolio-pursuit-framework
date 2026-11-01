# postgres-impl-pgvector-similarity : Anti-Patterns

Each anti-pattern : cause, symptom, fix. Most pgvector faults are **silent** , the query returns rows, just slowly or with poor recall, with no error. Verified against github.com/pgvector/pgvector (Last Verified : 2026-05-19).

## 1. Index operator class does not match the query operator

CAUSE : the index is built with one distance metric (`vector_cosine_ops`) but the query uses a different operator (`<->`).
SYMPTOM : `EXPLAIN` shows a `Seq Scan` despite the index existing ; query latency does not improve.
SQLSTATE : none , a silent performance fault.
FIX : build the index with the operator class that matches the query operator, one index per metric.

```sql
-- query uses <=>  -> index must use vector_cosine_ops
CREATE INDEX ON documents USING hnsw (embedding vector_cosine_ops);
SELECT id FROM documents ORDER BY embedding <=> $1 LIMIT 10;
```

## 2. ivfflat index built on an empty or tiny table

CAUSE : an ivfflat index was created before representative rows were loaded.
SYMPTOM : recall is poor , relevant rows are missing from results , because the k-means centroids trained on no (or too few) vectors.
SQLSTATE : none.
FIX : load representative data first, then create the ivfflat index. Or use HNSW, which needs no training.

```sql
-- 1. load embeddings   2. THEN build the index
CREATE INDEX ON documents USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
```

## 3. Embedding exceeds the indexable dimension limit

CAUSE : a `vector` column has more than 2000 dimensions and an index is expected.
SYMPTOM : `CREATE INDEX` on the `vector` column fails, or queries Seq Scan because no index exists.
SQLSTATE : error on `CREATE INDEX` , "column cannot have more than 2000 dimensions for hnsw/ivfflat index".
FIX : index the `halfvec` cast (indexable up to 4000 dimensions) ; the query must repeat the same cast.

```sql
CREATE INDEX ON documents USING hnsw ((embedding::halfvec(3072)) halfvec_cosine_ops);
SELECT id FROM documents ORDER BY embedding::halfvec(3072) <=> $1::halfvec(3072) LIMIT 10;
```

## 4. Inner product on non-normalized vectors

CAUSE : the query uses `<#>` inner product but the embeddings are not unit-normalized.
SYMPTOM : ranking is dominated by vector magnitude, not direction , long vectors always rank first regardless of semantic similarity.
SQLSTATE : none.
FIX : use `<=>` cosine distance (scale-independent), or normalize the embeddings to unit length before storing and then `<#>` equals cosine ordering.

## 5. Reading `<#>` as a positive similarity score

CAUSE : `<#>` returns the **negative** inner product (so that ascending index order yields most-similar-first).
SYMPTOM : the reported "score" is negative and looks wrong ; comparisons against a positive threshold fail.
SQLSTATE : none.
FIX : multiply by `-1` to recover the real inner product.

```sql
SELECT id, (embedding <#> $1) * -1 AS inner_product
FROM documents ORDER BY embedding <#> $1 LIMIT 10;
```

## 6. Distance threshold without ORDER BY ... LIMIT

CAUSE : the query filters by a distance threshold (`WHERE embedding <=> $1 < 0.3`) with no `ORDER BY ... LIMIT k`.
SYMPTOM : `Seq Scan` , the ANN index only accelerates the k-nearest-neighbour shape, not a threshold filter.
SQLSTATE : none.
FIX : use `ORDER BY embedding <=> $1 LIMIT k`. If a threshold is also needed, apply it in an outer query over the k-NN result.

```sql
SELECT * FROM (
    SELECT id, embedding <=> $1 AS distance
    FROM documents ORDER BY embedding <=> $1 LIMIT 100
) t WHERE distance < 0.3;
```

## 7. Filtered query expecting exactly LIMIT rows

CAUSE : a `WHERE` filter is combined with vector search ; the ANN index returns its nearest candidates, then the filter discards non-matching rows.
SYMPTOM : the query returns fewer than `LIMIT` rows when the filter is selective.
SQLSTATE : none.
FIX : use a partial index restricted to the filter predicate, or enable iterative scans (pgvector 0.8+).

```sql
CREATE INDEX ON documents USING hnsw (embedding vector_cosine_ops)
    WHERE status = 'published';
-- or :  SET hnsw.iterative_scan = strict_order;
```

## 8. One index expected to serve two distance metrics

CAUSE : a single index is built, but queries use both `<=>` and `<->`.
SYMPTOM : only queries matching the index opclass are accelerated ; the other metric Seq Scans.
SQLSTATE : none.
FIX : build one index per distance metric the application uses.

## 9. Mixed-dimension vectors in one column

CAUSE : the column is declared `vector` with no dimension, and rows of different lengths were inserted.
SYMPTOM : distance operators error at query time on a dimension mismatch.
SQLSTATE : error , "different vector dimensions".
FIX : declare a fixed dimension `vector(n)` so inserts are validated at write time.

## 10. Changing embedding dimensions without rebuilding the index

CAUSE : the embedding model changed (different dimension count) but the existing index was kept.
SYMPTOM : queries error or return wrong results ; the index no longer matches the column.
SQLSTATE : varies (dimension-mismatch error).
FIX : drop the index, `ALTER COLUMN ... TYPE vector(new_n)`, recreate the index.

```sql
DROP INDEX idx_documents_embedding_hnsw;
ALTER TABLE documents ALTER COLUMN embedding TYPE vector(1024);
CREATE INDEX CONCURRENTLY idx_documents_embedding_hnsw
    ON documents USING hnsw (embedding vector_cosine_ops);
```

## 11. Adding raw distance and ts_rank for hybrid search

CAUSE : hybrid search adds a cosine distance and a `ts_rank` score directly.
SYMPTOM : one signal dominates the combined score ; ranking is meaningless , the two scales are not comparable.
SQLSTATE : none.
FIX : fuse the two result lists by RANK position with Reciprocal Rank Fusion (`1 / (60 + rank)`), which is scale-independent.

## Source URLs

- https://github.com/pgvector/pgvector
- https://www.postgresql.org/docs/17/textsearch-controls.html
