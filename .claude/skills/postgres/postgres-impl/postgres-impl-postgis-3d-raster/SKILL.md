---
name: postgres-impl-postgis-3d-raster
description: >
  Use when working with 3D geometry, volumetric calculations, or raster (gridded) data in PostGIS.
  Prevents calling 3D functions without postgis_sfcgal installed (function not found), bloating tables with in-db rasters that should be out-db, and choosing raster where vector fits.
  Covers postgis_sfcgal 3D predicates (ST_3DIntersects, ST_3DDistance, ST_Volume), ST_Extrude, Z-coordinate handling, postgis_raster type (ST_Value, ST_Band, ST_Clip), in-db vs out-db storage, raster vs vector decision, postgis_topology overview.
  Keywords: PostGIS 3D, postgis_sfcgal, ST_3DDistance, ST_3DIntersects, ST_Extrude, ST_Volume, raster, postgis_raster, ST_Value, ST_Clip, postgis_topology, function does not exist sfcgal, 3D query, raster vs vector, gridded data, volumetric
license: MIT
compatibility: "Designed for Claude Code. Requires PostgreSQL 15, 16, or 17 with PostGIS 3.3+ plus postgis_sfcgal and postgis_raster extensions."
metadata:
  author: OpenAEC-Foundation
  version: "1.0"
---

# postgres-impl-postgis-3d-raster

## Quick Reference :

PostGIS splits beyond planar vector geometry into two extra worlds, each behind its own extension. **3D solids and volumetric operations** live in `postgis_sfcgal` (the SFCGAL backend). **Gridded / raster data** (elevation models, satellite imagery) lives in `postgis_raster`. A third extension, `postgis_topology`, models shared-boundary networks. None of these three is enabled by `CREATE EXTENSION postgis` alone : you must install each one explicitly.

The single most misunderstood point : NOT every `ST_3D*` function needs `postgis_sfcgal`. The 3D distance / intersection family (`ST_3DDistance`, `ST_3DIntersects`, `ST_3DDWithin`, `ST_3DClosestPoint`) is CORE PostGIS in 3.x : their SFCGAL implementations were removed in PostGIS 3.0.0 and replaced by the GEOS backend. They work with only `CREATE EXTENSION postgis`. What genuinely needs `postgis_sfcgal` is the SOLID and surface-construction family : `ST_Volume`, `ST_3DArea`, `ST_Extrude`, `ST_MakeSolid`, `ST_IsSolid`, `ST_Tesselate`, `ST_3DConvexHull`. Calling one of those without the extension raises SQLSTATE `42883` (undefined_function).

For raster, the dominant decision is in-db versus out-db storage. In-db stores pixels inside the table : fast pixel math, but a few large images bloat the table catastrophically. Out-db (`raster2pgsql -R`) stores only a file path plus metadata : no bloat, cloud-friendly, but needs `postgis.enable_outdb_rasters` turned on. ALWAYS tile rasters with `raster2pgsql -t` regardless of storage mode : an untiled raster is a single multi-gigabyte row. The data-model rule : continuous fields (elevation, temperature) are raster ; discrete features (parcels, roads) are vector.

## When To Use This Skill :

ALWAYS use this skill when :
- Computing volumes, 3D areas, or extruding 2D footprints into solids
- Running 3D spatial predicates (`ST_3DIntersects`, `ST_3DDistance`, `ST_3DDWithin`)
- Deciding whether a 3D function needs `postgis_sfcgal`
- Storing or querying raster / gridded data (DEMs, imagery, heatmaps)
- Choosing in-db vs out-db raster storage, or loading with `raster2pgsql`
- Combining raster and vector data (`ST_Clip`, `ST_Value`, raster `ST_Intersection`)
- Deciding between a raster and a vector data model
- Evaluating `postgis_topology` for shared-boundary datasets

NEVER use this skill for :
- 2D geometry, SRID, planar predicates, GiST spatial indexing : see `postgres-impl-postgis-geometry-2d`
- General index type selection : see `postgres-impl-indexing-strategy`
- Vector similarity search with embeddings : that is `pgvector`, unrelated to spatial raster

## Decision Trees :

### Raster or vector data model? :

```
What does the data represent?
├── A CONTINUOUS field sampled on a grid
│   (elevation, temperature, rainfall, satellite bands, slope) :
│       Raster. postgis_raster. One value per pixel per band.
└── DISCRETE features with identity and attributes
    (parcels, roads, buildings, sensor points) :
        Vector geometry. See postgres-impl-postgis-geometry-2d.

Need BOTH (e.g. "average elevation inside each parcel")?
    Keep elevation as raster, parcels as vector, combine with
    ST_Clip(rast, geom) or ST_Value / raster ST_Intersection.
```

### Does this 3D function need postgis_sfcgal? :

```
Which 3D function?
├── 3D distance / intersection family :
│   ST_3DDistance, ST_3DIntersects, ST_3DDWithin, ST_3DClosestPoint
│       CORE PostGIS 3.x. SFCGAL backend was REMOVED in 3.0.0.
│       Works with just CREATE EXTENSION postgis. No sfcgal needed.
├── Z-coordinate accessors / forcers :
│   ST_Z, ST_Force3D, ST_Force2D, ST_MakePoint(x,y,z)
│       CORE PostGIS. No sfcgal needed.
└── Solid + surface construction family :
    ST_Volume, ST_3DArea, ST_Extrude, ST_MakeSolid, ST_IsSolid,
    ST_Tesselate, ST_3DConvexHull
        REQUIRES postgis_sfcgal. Without it -> SQLSTATE 42883
        (undefined_function). Run CREATE EXTENSION postgis_sfcgal.
```

### In-db or out-db raster storage? :

```
How large is the raster data and where does it live?
├── Small / derived rasters, heavy pixel-level math, must be
│   self-contained in the DB :
│       In-db (raster2pgsql default, no -R). Pixels stored in
│       the table. ALWAYS tile with -t to avoid giant rows.
└── Large imagery / DEM coverages, files already on disk or in
    cloud object storage, table-bloat is a concern :
        Out-db (raster2pgsql -R). Table stores only file path +
        metadata. Enable with:
          SET postgis.enable_outdb_rasters = true;
          SET postgis.gdal_enabled_drivers TO 'ENABLE_ALL';
        ALWAYS still tile with -t.
```

## Patterns :

### Pattern 1 : Install the right extension before 3D or raster work

ALWAYS run the matching `CREATE EXTENSION` before using SFCGAL, raster, or topology functions.
NEVER assume `CREATE EXTENSION postgis` enables 3D solids, raster, or topology : it does not.

```sql
CREATE EXTENSION IF NOT EXISTS postgis;            -- base : 2D + core 3D predicates
CREATE EXTENSION IF NOT EXISTS postgis_sfcgal;     -- ST_Volume, ST_Extrude, ST_MakeSolid...
CREATE EXTENSION IF NOT EXISTS postgis_raster;     -- raster type + ST_Value / ST_Clip
CREATE EXTENSION IF NOT EXISTS postgis_topology;   -- TopoGeometry + topology schema

-- Confirm what is installed :
SELECT extname FROM pg_extension WHERE extname LIKE 'postgis%';
```

WHY : `postgis_sfcgal`, `postgis_raster`, and `postgis_topology` are separate, independently versioned extensions packaged with PostGIS but not auto-enabled. Calling their functions without the extension raises SQLSTATE `42883` (undefined_function), which reads as "function does not exist" and is easy to misdiagnose as a typo. Source : postgis.net/docs/manual-3.4 (Installation ; Raster Data Management ; Topology).

### Pattern 2 : Know the SFCGAL boundary for 3D functions

ALWAYS check the function family before assuming a 3D call needs `postgis_sfcgal`.
NEVER install `postgis_sfcgal` just to run `ST_3DDistance` or `ST_3DIntersects` : in PostGIS 3.x those are core GEOS-backed functions.

```sql
-- CORE PostGIS 3.x : no postgis_sfcgal required.
SELECT ST_3DDistance(
  'POINT Z(0 0 0)'::geometry,
  'POINT Z(0 0 10)'::geometry);                    -- 10

SELECT ST_3DIntersects(
  'LINESTRING Z(0 0 0, 0 0 5)'::geometry,
  'POINT Z(0 0 3)'::geometry);                     -- true

-- REQUIRES postgis_sfcgal : fails with 42883 if the extension is absent.
SELECT ST_Volume(ST_MakeSolid(geom));              -- needs sfcgal
SELECT ST_Extrude(geom, 0, 0, 10);                 -- needs sfcgal
```

WHY : PostGIS 3.0.0 removed the SFCGAL implementations of the 3D distance / intersection family and reimplemented them on the GEOS backend (`ST_3DIntersects` now also supports TINs). The solid-construction family (`ST_Volume`, `ST_3DArea`, `ST_Extrude`, `ST_MakeSolid`, `ST_IsSolid`, `ST_Tesselate`) was never migrated and still depends on the SFCGAL library. Source : postgis.net/docs/manual-3.4/ST_3DDistance.html, ST_3DIntersects.html, ST_Volume.html, ST_Extrude.html.

### Pattern 3 : Extrude a 2D footprint into a 3D solid

ALWAYS use `ST_Extrude(geom, x, y, z)` to sweep a 2D surface into a 3D polyhedral surface.
NEVER expect `ST_Extrude` to return a closed solid directly : it returns a polyhedral surface ; wrap it in `ST_MakeSolid` before measuring volume.

```sql
-- Extrude a building footprint 10 metres straight up. (PostGIS 3.3+, needs sfcgal)
SELECT ST_Extrude(footprint, 0, 0, 10) AS shell
FROM building;

-- Footprint -> shell -> solid -> volume, in one chain.
SELECT ST_Volume(
         ST_MakeSolid(
           ST_Extrude(footprint, 0, 0, height_m)))  AS volume_m3
FROM building;
```

WHY : `ST_Extrude` extends a 2D geometry along the x/y/z vector and produces a polyhedral surface (it preserves the Z dimension). `ST_Volume` returns 0 for any surface, even a closed one : it computes a non-zero volume only for a true solid. `ST_MakeSolid` reinterprets a closed polyhedral surface as a solid without revalidating it. Source : postgis.net/docs/manual-3.4/ST_Extrude.html, ST_Volume.html.

### Pattern 4 : Out-db storage plus tiling for large rasters

ALWAYS tile rasters with `raster2pgsql -t` and prefer `-R` (out-db) for large imagery and DEM coverages.
NEVER load a multi-gigabyte image untiled and in-db : it becomes one enormous row that bloats the table and is unusable by the spatial index.

```bash
# Out-db, tiled, with constraints and a spatial index. (raster2pgsql)
raster2pgsql -s 4326 -t 256x256 -I -C -R /data/dem/*.tif public.dem \
  | psql -d gisdb

# In-db variant (omit -R) for small derived rasters :
raster2pgsql -s 4326 -t 100x100 -I -C /data/small.tif public.heat \
  | psql -d gisdb
```

```sql
-- Out-db rasters need this enabled in the session / config :
SET postgis.enable_outdb_rasters = true;
SET postgis.gdal_enabled_drivers TO 'ENABLE_ALL';
```

WHY : `-t WIDTHxHEIGHT` cuts the source into one tile per table row so the GiST index can prune by tile ; without it the whole coverage is a single row. `-R` registers the raster as a filesystem (out-db) raster : the table holds only the file path and metadata, eliminating table bloat and allowing cloud-hosted files. Out-db trades a small read overhead for a massive storage and load-time win. Source : postgis.net/docs/manual-3.4 (Raster Data Management ; raster2pgsql).

### Pattern 5 : Combine raster and vector with ST_Clip and ST_Value

ALWAYS clip a raster to a vector boundary with `ST_Clip(rast, geom)` for "what is inside this polygon" questions.
NEVER convert a whole raster to vector points just to sample one location : use `ST_Value(rast, band, point)`.

```sql
-- Sample the elevation raster at a single sensor point. (postgis_raster)
SELECT ST_Value(d.rast, 1, s.geom) AS elevation_m
FROM dem d
JOIN sensor s ON ST_Intersects(d.rast, s.geom);

-- Mean elevation inside each parcel : clip the DEM to the parcel polygon.
SELECT p.id,
       (ST_SummaryStats(ST_Clip(d.rast, p.geom))).mean AS avg_elev
FROM parcel p
JOIN dem d ON ST_Intersects(d.rast, p.geom)
GROUP BY p.id, d.rast, p.geom;
```

WHY : `ST_Intersects(raster, geometry)` is the index-aware filter that pairs raster tiles with overlapping vectors. `ST_Value` reads a single pixel under a point ; `ST_Clip` cuts the raster down to a polygon so per-pixel aggregates only cover the area of interest. Both keep the data in its native model instead of an expensive raster-to-vector conversion. Source : postgis.net/docs/manual-3.4 (Raster Reference : ST_Value, ST_Clip, ST_Intersects).

## Anti-Patterns :

(Short list ; full cause / symptom / fix in references/anti-patterns.md)

- Calling `ST_Volume` / `ST_Extrude` / `ST_MakeSolid` without `postgis_sfcgal` : SQLSTATE `42883`
- Installing `postgis_sfcgal` to run `ST_3DDistance` : unnecessary, those are core in 3.x
- `ST_Volume` on a polyhedral surface : returns 0, must `ST_MakeSolid` first
- Large in-db untiled raster : one giant row, table bloat, unusable index
- Out-db raster query with `postgis.enable_outdb_rasters` off : pixel reads fail
- Raster used for discrete features (or vector for continuous fields) : wrong model
- Mixing 2D and 3D geometries in a 3D predicate without forcing dimensions : silent wrong results
- Forgetting `-t` tiling on `raster2pgsql` : no spatial pruning possible

## Reference Links :

- [references/methods.md](references/methods.md) : Full API surface, SFCGAL and raster function signatures, `raster2pgsql` flags
- [references/examples.md](references/examples.md) : Working code samples, version-annotated, 3D and raster end-to-end
- [references/anti-patterns.md](references/anti-patterns.md) : Anti-patterns with cause, symptom, SQLSTATE, and fix

## See Also :

- `postgres-impl-postgis-geometry-2d` : 2D geometry, SRID, GiST indexing, planar predicates ; the prerequisite skill
- `postgres-impl-indexing-strategy` : GiST / SP-GiST / BRIN selection for spatial columns
- Vooronderzoek section : §10 (PostGIS : 3D operations, raster support, topology)
- Official docs : https://postgis.net/docs/manual-3.4/
