# Methods : PostGIS 2D Geometry and Geography

Full API surface. Verified against the PostGIS 3.4 manual
(using_postgis_dbmanagement.html, ST_DWithin.html, ST_Transform.html,
ST_Intersects.html). Baseline is PostGIS 3.3+ ; 3.4-only features are marked.

## Extension and Column Setup

| Statement | Purpose |
|-----------|---------|
| `CREATE EXTENSION postgis;` | Install the core PostGIS extension into the database |
| `CREATE EXTENSION postgis_sfcgal;` | Optional : true 3D geometry (`ST_3D*`, volumes) |
| `CREATE EXTENSION postgis_raster;` | Optional : gridded raster data |
| `CREATE EXTENSION postgis_topology;` | Optional : topological data model |

`CREATE EXTENSION` is per database, not per cluster. Run it once.

## Spatial Column Types

| Type declaration | Meaning |
|------------------|---------|
| `geometry(Point, 4326)` | Planar geometry, point only, SRID 4326 enforced |
| `geometry(LineString, 3857)` | Planar line, SRID 3857 enforced |
| `geometry(Polygon, 28992)` | Planar polygon, projected national-grid SRID |
| `geometry(MultiPolygon, 4326)` | Planar multi-polygon |
| `geometry` | Bare : accepts any type, any SRID (avoid : no enforcement) |
| `geography(Point, 4326)` | Spheroidal point on WGS84, distances in meters |
| `geography(Polygon, 4326)` | Spheroidal polygon |

`geometry` math is planar : fast, full function set, units are the SRID units.
`geography` math is spheroidal on WGS84 : slower, smaller function set,
distances and lengths always in meters, correct across poles and antimeridian.

## SRID and Reference System

| Function / table | Signature | Purpose |
|------------------|-----------|---------|
| `spatial_ref_sys` | table | Catalog of every known SRID and its definition |
| `ST_SRID` | `ST_SRID(geometry) -> integer` | Read the SRID label of a geometry |
| `ST_SetSRID` | `ST_SetSRID(geometry, integer) -> geometry` | Relabel the SRID ; coordinates UNCHANGED |
| `ST_Transform` | `ST_Transform(geometry, integer) -> geometry` | Reproject : RECOMPUTE coordinates into a new SRID |
| `ST_Transform` | `ST_Transform(geometry, text to_proj) -> geometry` | Reproject to a PROJ string target |
| `ST_Transform` | `ST_Transform(geometry, text from_proj, text to_proj)` | Reproject specifying both ends |

Common SRIDs : `4326` = WGS84 lat/lon ; `3857` = Web Mercator (tile servers) ;
projected national grids give accurate planar geometry over a limited area.
`ST_Transform` requires the source geometry to already carry a valid SRID.

## Index Methods

| Index statement | Use |
|-----------------|-----|
| `CREATE INDEX i ON t USING GIST (geom);` | Standard 2D spatial index (default choice) |
| `CREATE INDEX CONCURRENTLY i ON t USING GIST (geom);` | Build without a write lock on a live table |
| `CREATE INDEX i ON t USING GIST (geom gist_geometry_ops_nd);` | N-dimensional (3D / 4D) index |
| `CREATE INDEX i ON t USING SPGIST (geom);` | SP-GiST k-d tree, point-heavy datasets |
| `CREATE INDEX i ON t USING BRIN (geom);` | BRIN, huge tables inserted in spatial order |

Run `ANALYZE t;` after a bulk load so the planner sees current statistics.

## Constructors

| Function | Signature | Notes |
|----------|-----------|-------|
| `ST_MakePoint` | `ST_MakePoint(x, y [, z [, m]]) -> geometry` | Returns SRID 0 ; wrap in `ST_SetSRID` |
| `ST_Point` | `ST_Point(x, y [, srid]) -> geometry` | 3-arg form sets SRID directly (v3.2+) |
| `ST_GeomFromText` | `ST_GeomFromText(wkt text, srid integer) -> geometry` | Parse WKT, assign SRID |
| `ST_GeogFromText` | `ST_GeogFromText(ewkt text) -> geography` | Parse text into a geography |
| `ST_GeomFromGeoJSON` | `ST_GeomFromGeoJSON(json text) -> geometry` | Parse a GeoJSON geometry object |
| `ST_MakeEnvelope` | `ST_MakeEnvelope(xmin, ymin, xmax, ymax [, srid]) -> geometry` | Rectangle from corner coordinates |
| EWKT literal | `'SRID=4326;POINT(x y)'::geometry` | Inline SRID in the text literal |

`ST_MakePoint` and 2-arg `ST_Point` produce SRID 0. Always `ST_SetSRID` the
result before inserting into a typmod-constrained column.

## Spatial Relationship Predicates

All return `boolean`. Each starts with a bounding-box `&&` test, so a GiST
index on either operand accelerates the query.

| Function | Signature | True when |
|----------|-----------|-----------|
| `ST_Intersects` | `(geometry, geometry) -> boolean` | Geometries share at least one point |
| `ST_Intersects` | `(geography, geography) -> boolean` | Geography variant |
| `ST_Disjoint` | `(geometry, geometry) -> boolean` | Geometries share NO point (no index `&&` shortcut) |
| `ST_Within` | `(geometry A, geometry B) -> boolean` | A lies entirely inside B |
| `ST_Contains` | `(geometry A, geometry B) -> boolean` | B lies entirely inside A (inverse of Within) |
| `ST_Covers` | `(geometry A, geometry B) -> boolean` | Every point of B is in A (boundary-inclusive) |
| `ST_CoveredBy` | `(geometry A, geometry B) -> boolean` | Inverse of Covers |
| `ST_Overlaps` | `(geometry, geometry) -> boolean` | Same dimension, share space, neither contains the other |
| `ST_Touches` | `(geometry, geometry) -> boolean` | Share only a boundary point, no interior overlap |
| `ST_Crosses` | `(geometry, geometry) -> boolean` | Interiors cross with lower-dimension intersection |
| `ST_Equals` | `(geometry, geometry) -> boolean` | Geometries are spatially identical |

`ST_Disjoint` cannot use the index `&&` shortcut : prefer
`NOT ST_Intersects(a, b)` when an index exists.

## Distance and Proximity

| Function | Signature | Index? | Notes |
|----------|-----------|--------|-------|
| `ST_DWithin` | `ST_DWithin(geometry, geometry, distance) -> boolean` | YES | Distance in SRID units |
| `ST_DWithin` | `ST_DWithin(geography, geography, distance_m [, use_spheroid]) -> boolean` | YES | Distance in METERS |
| `ST_Distance` | `ST_Distance(geometry, geometry) -> double precision` | NO (alone) | SRID units |
| `ST_Distance` | `ST_Distance(geography, geography [, use_spheroid]) -> double precision` | NO (alone) | METERS |
| `ST_DistanceSphere` | `ST_DistanceSphere(geometry, geometry) -> double precision` | NO | Fast sphere approximation, meters |
| `<->` | `geometry <-> geometry -> double precision` | YES (KNN) | Index-assisted nearest-neighbour ordering |

ALWAYS filter proximity with `ST_DWithin` (bounding-box phase uses GiST). Use
`ST_Distance` only to rank an already-filtered small set. Use `<->` in
`ORDER BY ... LIMIT k` for K-nearest-neighbour queries.

## Measurement

| Function | Signature | Returns |
|----------|-----------|---------|
| `ST_Area` | `ST_Area(geometry) -> double precision` | Area in SRID units squared (meters^2 for geography) |
| `ST_Length` | `ST_Length(geometry) -> double precision` | Length of lines in SRID units (meters for geography) |
| `ST_Perimeter` | `ST_Perimeter(geometry) -> double precision` | Perimeter of polygons |
| `ST_Centroid` | `ST_Centroid(geometry) -> geometry` | Geometric center point |
| `ST_Envelope` | `ST_Envelope(geometry) -> geometry` | Axis-aligned bounding box as a polygon |

For real-world meters on a `geometry`, `ST_Transform` to a projected SRID
first, or cast to `geography`.

## Processing and Construction

| Function | Signature | Purpose |
|----------|-----------|---------|
| `ST_Buffer` | `ST_Buffer(geometry, radius) -> geometry` | Polygon offset by `radius` (SRID units) |
| `ST_Buffer` | `ST_Buffer(geography, radius_m) -> geography` | Buffer in meters |
| `ST_Intersection` | `ST_Intersection(geometry, geometry) -> geometry` | Shared geometry of two inputs |
| `ST_Difference` | `ST_Difference(geometry, geometry) -> geometry` | First minus second |
| `ST_Union` | `ST_Union(geometry) -> geometry` (aggregate) | Dissolve many geometries into one |
| `ST_Simplify` | `ST_Simplify(geometry, tolerance) -> geometry` | Reduce vertex count |
| `ST_MakeEnvelope` | `ST_MakeEnvelope(xmin,ymin,xmax,ymax[,srid])` | Build a rectangle |

## Clustering Window Functions

| Function | Signature | Purpose |
|----------|-----------|---------|
| `ST_ClusterDBSCAN` | `ST_ClusterDBSCAN(geometry, eps, minpoints) OVER () -> integer` | Density-based cluster id per row |
| `ST_ClusterKMeans` | `ST_ClusterKMeans(geometry, k [, max_radius]) OVER () -> integer` | K-means cluster id per row |

Both are window functions : they require an `OVER ()` clause and assign a
cluster id to each input row. `max_radius` for `ST_ClusterKMeans` is v3.1+.

## Output

| Function | Signature | Returns |
|----------|-----------|---------|
| `ST_AsText` | `ST_AsText(geometry) -> text` | WKT representation |
| `ST_AsGeoJSON` | `ST_AsGeoJSON(geometry) -> text` | GeoJSON representation |
| `ST_AsEWKT` | `ST_AsEWKT(geometry) -> text` | WKT with the `SRID=...;` prefix |
