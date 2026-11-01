# Anti-Patterns : postgres-impl-postgis-3d-raster

Each entry : cause, symptom, SQLSTATE where applicable, and the fix pattern.
Verified against the PostGIS 3.4 manual.

## AP-1 : SFCGAL function called without postgis_sfcgal

CAUSE : calling `ST_Volume`, `ST_Extrude`, `ST_3DArea`, `ST_MakeSolid`,
`ST_IsSolid`, `ST_Tesselate`, or `ST_3DConvexHull` when only `CREATE EXTENSION
postgis` has been run.

SYMPTOM : the statement fails with SQLSTATE `42883` (undefined_function) and a
message such as `function st_volume(geometry) does not exist`. Easy to misread as
a typo or a version problem.

```sql
SELECT ST_Volume(geom);   -- ERROR 42883 : function st_volume(geometry) does not exist
```

FIX : install the SFCGAL extension once per database.

```sql
CREATE EXTENSION IF NOT EXISTS postgis_sfcgal;
```

## AP-2 : Installing postgis_sfcgal for core 3D functions

CAUSE : believing every `ST_3D*` function needs SFCGAL, and installing
`postgis_sfcgal` only to run `ST_3DDistance`, `ST_3DIntersects`, `ST_3DDWithin`,
or `ST_3DClosestPoint`.

SYMPTOM : not an error, but a wrong mental model and an unnecessary extension.
PostGIS 3.0.0 removed the SFCGAL backend for the 3D distance / intersection family
and reimplemented it on GEOS ; those functions are core.

FIX : run the 3D distance / intersection family with just `CREATE EXTENSION
postgis`. Reserve `postgis_sfcgal` for the solid-construction family.

## AP-3 : ST_Volume on a surface instead of a solid

CAUSE : calling `ST_Volume` on a polyhedral surface (for example the direct output
of `ST_Extrude`), expecting a non-zero volume.

SYMPTOM : `ST_Volume` silently returns `0`. No error is raised, so the bug surfaces
later as zero volumes throughout a report.

```sql
SELECT ST_Volume(ST_Extrude(footprint, 0, 0, 10));   -- 0  (surface, not solid)
```

FIX : convert the closed surface to a solid with `ST_MakeSolid` first.

```sql
SELECT ST_Volume(ST_MakeSolid(ST_Extrude(footprint, 0, 0, 10)));   -- non-zero
```

## AP-4 : Large in-db untiled raster

CAUSE : loading a multi-gigabyte image with `raster2pgsql` without `-t` (tiling)
and without `-R` (out-db), so the whole coverage lands as one in-db row.

SYMPTOM : a single enormous table row ; the GiST spatial index cannot prune ; every
raster query reads the entire image ; the table bloats and backups balloon.

FIX : always tile with `-t`, and use `-R` (out-db) for large coverages.

```bash
raster2pgsql -s 4326 -t 256x256 -I -C -R /data/big.tif public.dem | psql -d gisdb
```

## AP-5 : Out-db raster query with out-db rasters disabled

CAUSE : querying pixel values of an out-db raster while
`postgis.enable_outdb_rasters` is off (its default).

SYMPTOM : functions like `ST_Value` and `ST_Clip` return `NULL` or raise an error
about out-db access being disabled, even though the raster row exists.

FIX : enable out-db access (per session, or persist in config).

```sql
SET postgis.enable_outdb_rasters = true;
SET postgis.gdal_enabled_drivers TO 'ENABLE_ALL';
```

## AP-6 : Wrong data model, raster vs vector

CAUSE : storing a continuous field (elevation, temperature) as millions of discrete
vector points, or storing discrete features (parcels, roads) as a raster.

SYMPTOM : continuous-field-as-vector bloats storage, has no native grid math, and
is slow to sample. Discrete-features-as-raster loses feature identity and
attributes, and cannot answer per-feature questions.

FIX : continuous fields are raster (`postgis_raster`) ; discrete features are vector
geometry. When a query needs both, keep each in its native model and combine with
`ST_Clip` / `ST_Value` / raster `ST_Intersection`.

## AP-7 : Mixing 2D and 3D geometries in a 3D predicate

CAUSE : passing a 2D geometry (no Z) and a 3D geometry into `ST_3DDistance` or
`ST_3DIntersects`.

SYMPTOM : no error, but the 2D geometry is treated as Z = 0, producing distances
and intersection results that are silently wrong for data whose real Z is not 0.

FIX : force a consistent dimensionality before the predicate. Promote 2D inputs
with `ST_Force3D(geom, z)` using a meaningful Z, or drop to 2D deliberately with
`ST_Force2D` if the Z is genuinely irrelevant.

## AP-8 : raster2pgsql without a spatial index

CAUSE : loading raster tiles without `-I`, so the raster column has no GiST index.

SYMPTOM : `ST_Intersects(raster, geometry)` joins and bounding-box filters do a
full scan of every tile. Fine on a few tiles, slow on a large coverage.

FIX : pass `-I` to `raster2pgsql` (or `CREATE INDEX ... USING gist` on the raster
column afterwards) so tile-level pruning works.

## AP-9 : Topology used where plain geometry suffices

CAUSE : reaching for `postgis_topology` and `TopoGeometry` for a dataset whose
features do not share boundaries.

SYMPTOM : extra schema complexity (the `topology` schema, node/edge/face tables)
with no benefit, because there are no shared edges to deduplicate.

FIX : use `postgis_topology` only when features genuinely share boundaries and
topological consistency matters (cadastral parcels, census tracts, road networks).
Otherwise use plain `geometry` columns.

## Sources

- https://postgis.net/docs/manual-3.4/
- https://postgis.net/docs/manual-3.4/ST_3DDistance.html
- https://postgis.net/docs/manual-3.4/ST_Volume.html
- https://postgis.net/docs/manual-3.4/ST_Extrude.html
- https://postgis.net/docs/manual-3.4/using_raster_dataman.html
- https://postgis.net/docs/manual-3.4/Topology.html
- https://www.postgresql.org/docs/17/errcodes-appendix.html
