# Anti-Patterns : PostGIS 2D Geometry and Geography

Each entry : cause, symptom, fix-pattern, and the error message / SQLSTATE
where one applies. Verified against the PostGIS 3.4 manual.

## AP-1 : ST_Distance in a WHERE clause instead of ST_DWithin

**Cause** : a proximity filter is written as
`WHERE ST_Distance(geom, point) < radius`.

**Symptom** : `EXPLAIN` shows a `Seq Scan` with `Filter: (st_distance(...) <
...)` ; query time grows linearly with row count even though a GiST index
exists.

**Fix** : use `ST_DWithin`, which begins with a bounding-box `&&` step that the
GiST index can serve.

```sql
-- WRONG : index skipped, full scan
SELECT id FROM place
WHERE ST_Distance(geom, ST_SetSRID(ST_MakePoint(4.9,52.37),4326)) < 0.05;

-- RIGHT : GiST-indexed
SELECT id FROM place
WHERE ST_DWithin(geom, ST_SetSRID(ST_MakePoint(4.9,52.37),4326), 0.05);
```

**SQLSTATE** : none ; results are correct, the query is just slow.

## AP-2 : Missing GiST index on a spatial column

**Cause** : a `geometry` or `geography` column is queried with `ST_Intersects`,
`ST_Within`, or `ST_DWithin` but has no spatial index.

**Symptom** : every spatial query is a `Seq Scan` ; performance collapses as
the table grows. The bounding-box `&&` test in each predicate has nothing to
consult.

**Fix** : create a GiST index, then `ANALYZE`.

```sql
CREATE INDEX place_geom_gist ON place USING GIST (geom);
ANALYZE place;
```

**SQLSTATE** : none ; correct but unindexed results.

## AP-3 : Mixing SRIDs without ST_Transform

**Cause** : two geometries in different SRIDs are passed to the same spatial
predicate or operator.

**Symptom** : the query aborts with
`ERROR: Operation on mixed SRID geometries`. With a bare `geometry` column
that silently held mixed SRIDs, results can also be wrong instead of erroring.

**Fix** : reproject one operand with `ST_Transform` so both share a SRID, and
declare columns with a typmod to forbid mixed SRIDs at insert time.

```sql
-- WRONG : r.geom is 4326, d.geom is 28992
WHERE ST_Intersects(r.geom, d.geom)

-- RIGHT : align first
WHERE ST_Intersects(r.geom, ST_Transform(d.geom, ST_SRID(r.geom)))
```

**SQLSTATE** : `XX000` (internal error class) for the mixed-SRID abort.

## AP-4 : geography type for small local datasets

**Cause** : `geography` is chosen for a city-scale or region-scale dataset
because "distances are in meters".

**Symptom** : spatial queries are noticeably slower than necessary and some
PostGIS functions are unavailable, because `geography` runs spheroidal math and
supports a smaller function set.

**Fix** : for local data use `geometry` in a projected SRID (national grid or
3857). Measurements are then in meters and the planar math is fast.

```sql
-- For Netherlands-scale data, EPSG:28992 (RD New) gives meter accuracy
CREATE TABLE parcel (geom geometry(Polygon, 28992));
SELECT ST_Area(geom) AS area_m2 FROM parcel;   -- already meters^2
```

**SQLSTATE** : none ; a performance and design mismatch.

## AP-5 : Expecting meters from ST_Distance on geometry(Point,4326)

**Cause** : `ST_Distance` is called on a `geometry` column in SRID 4326 and the
result is treated as meters.

**Symptom** : distances come back as tiny numbers like `0.045` ; thresholds
behave nonsensically. SRID 4326 units are degrees of latitude/longitude, not
meters, and a degree is not a constant ground distance.

**Fix** : cast to `geography`, or `ST_Transform` to a projected SRID, before
measuring.

```sql
-- WRONG : result is in degrees
SELECT ST_Distance(a.geom, b.geom) FROM place a, place b;

-- RIGHT : meters via geography
SELECT ST_Distance(a.geom::geography, b.geom::geography) FROM place a, place b;
```

**SQLSTATE** : none ; the number is real but in the wrong unit.

## AP-6 : ST_MakePoint left at SRID 0 inserted into a typmod column

**Cause** : a point built with `ST_MakePoint(x, y)` is inserted directly into a
`geometry(Point, 4326)` column.

**Symptom** : the insert fails because `ST_MakePoint` returns SRID 0 and the
typmod requires SRID 4326.

**Fix** : wrap the constructor in `ST_SetSRID`, or use `ST_GeomFromText` /
`ST_Point` with an explicit SRID.

```sql
-- WRONG
INSERT INTO place (geom) VALUES (ST_MakePoint(4.9, 52.37));

-- RIGHT
INSERT INTO place (geom) VALUES (ST_SetSRID(ST_MakePoint(4.9, 52.37), 4326));
```

**SQLSTATE** : `22023` (`invalid_parameter_value`) : the geometry does not
match the column type modifier.

## AP-7 : Using ST_SetSRID to "fix" a wrong SRID

**Cause** : data was loaded with the wrong SRID label and someone applies
`ST_SetSRID` to "correct" it.

**Symptom** : the SRID label changes but the coordinates do not move, so the
geometry now claims a coordinate system its numbers do not belong to. Every
later distance, area, and overlay is silently wrong.

**Fix** : if the coordinates are correct for the original system, use
`ST_Transform` to actually reproject. Use `ST_SetSRID` only to label a geometry
that has SRID 0 and whose coordinates already match the target system.

```sql
-- WRONG : coordinates are 28992 meters but get relabelled as 4326 degrees
UPDATE parcel SET geom = ST_SetSRID(geom, 4326);

-- RIGHT : the geometry truly is 28992 ; reproject to 4326
UPDATE parcel SET geom = ST_Transform(ST_SetSRID(geom, 28992), 4326);
```

**SQLSTATE** : none ; produces silently corrupt spatial data.

## AP-8 : ST_Disjoint expecting an index speedup

**Cause** : "rows that do not touch X" is written as
`WHERE ST_Disjoint(geom, X)`.

**Symptom** : the query is slow even with a GiST index. `ST_Disjoint` is the
negation of intersection : no bounding box can prove disjointness cheaply, so
it has no `&&` index shortcut.

**Fix** : write `NOT ST_Intersects(geom, X)` so the indexed `&&` test runs and
the index narrows candidates before the negation is applied.

```sql
-- WRONG : no index shortcut
SELECT id FROM place WHERE ST_Disjoint(geom, :poly);

-- RIGHT : indexed
SELECT id FROM place WHERE NOT ST_Intersects(geom, :poly);
```

**SQLSTATE** : none ; correct but unindexed.
