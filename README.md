# Public Transport Study

Open the [interactive visual methods guide](README.html) for a browser-based walkthrough of urban-area growth, walking catchments, GTFS frequency, travel-time accessibility, publishing, and validation.

This repository processes urban, population, street, building, and public transport data for cities around the world. The current urban-area workflow first identifies a **morphological built-up urban area**: a contiguous area of urban fabric based on population density, building evidence, and local street fabric. That area is then used as a defensible population denominator and spatial frame for public transport coverage analysis.

The built-up area is not the same thing as a public transport service area. Stations and catchments should be evaluated with walking-catchment population logic, not by simply checking whether a station point lies inside the built-up polygon.

## Project Overview

The workflow has six main parts:

1. **Data preparation**
   - Population grid cells, currently expected as polygon cells with a `population` column.
   - City boundary or analysis envelope.
   - Building footprints, or point-building fallback data when footprints are unavailable.
   - OSM road/street data.
   - Rail, station, walking-network, and catchment data for downstream public transport analysis.

2. **Urban-area calculation**
   - Clip population cells to the analysis envelope with area-weighted population.
   - Aggregate buildings and roads to each cell.
   - Calculate urban metrics using population density, built-up coverage, and local street density.
   - Grow contiguous urban components from high-scoring seed cells.
   - Refine boundary cells into smaller subcells and allocate population dasymetrically where possible.
   - Write urban cells, a dissolved urban boundary, enriched analysis cells, validation, and provenance.

3. **Public transport catchment analysis** (`calc_transport.py`)
   - Build rail, metro and tram stations from GTFS parent stations served on a pinned school-term service date.
   - Use the urban area as the denominator and urban population mask.
   - Keep stations based on walking-catchment population/intersection criteria.
   - Dissolve station catchments before calculating total covered population to avoid double counting.

4. **GTFS service frequency and all-mode tier** (also `calc_transport.py`)
   - Count departures per hour in the busiest direction, 07:00–09:00, excluding each trip's final stop.
   - "Frequent" means at least six departures per hour in that direction (a 10-minute headway).
   - Add an all-mode tier (bus, tram, metro, rail, ferry): population within 500 m of any stop, and within reach of frequent service of any mode.

5. **Door-to-door travel time** (`calc_gtfs.py`)
   - Build an r5py network from each city OSM PBF and GTFS feed set.
   - Route population-cell centroids to the city centre and, by default in the batch run, to all other cells.
   - Report population-weighted centre travel times, 30/45-minute centre shares, and cumulative accessibility: the population-weighted share of all urban residents reachable within 30/45 minutes.

6. **Publication**
   - Harmonise all city schemas in equal-area EPSG:3035.
   - Atomically publish `Output/pt_study.gpkg` and `Output/city_summary.csv` for ArcGIS Pro and the report.

## Urban Area Concepts

| Concept | Meaning in this repository | Downstream use |
| --- | --- | --- |
| Morphological built-up area | Contiguous urban fabric identified from population density, building/built-up evidence, and local street fabric. | Main denominator and spatial frame for urban PT coverage metrics. |
| Functional/service area | The wider area served by a PT system or linked by commuting/service relationships. This can include satellites, corridors, and outlying settlements that are not continuous built-up fabric. | Station inclusion, service interpretation, and future commuting/FUA-style analysis. |

These are deliberately separated. A station can sit outside the built-up polygon but still serve urban population through its walking catchment. Conversely, a road or rail corridor can pass through non-urban land and should not by itself expand the built-up area.

## Repository Entry Points

| File | Purpose |
| --- | --- |
| `calc_urban_area.py` | Command-line runner for the reusable urban-area pipeline. Prefer this for production-style runs. |
| `calc_transport.py` | OSM rail, walk-network isochrone, station-filtering, and weighted-coverage runner. |
| `calc_gtfs.py` | GTFS frequency and optional r5py travel-time runner. |
| `run_study.py` | Resumable six-city stage runner with config/code fingerprints, per-city logs, and timings. |
| `publish_outputs.py` | Atomic EPSG:3035 publisher for the consolidated ArcGIS GeoPackage and summary CSV. |
| `README.html` | Self-contained interactive visual walkthrough of the full process. |
| `calc_urban_area.ipynb` | Thin notebook entry point plus older exploratory diagnostics. The top cells call `run_urban_area_pipeline`; package code is the source of truth. |
| `urban_area/` | Reusable implementation for config, I/O, scoring, growth, refinement, validation, and catchment helpers. |
| `city_config.example.json` | Six-city settings for Oslo, Stockholm, Copenhagen, Helsinki, Brussels, and Paris. |
| `tests/test_urban_area_units.py` | Synthetic geospatial unit tests for the key edge cases. |
| `get_ms_buildings.py` | Downloads Microsoft Global ML Building Footprints for a configured city. |
| `road-density.py` | Standalone road-density aggregation using the shared CRS and clipping logic. |

## Environment Setup

The repository includes a Conda environment file:

```powershell
conda env create -f environment.yml
conda activate pt_study
```

To update an existing environment after dependencies change:

```powershell
conda env update -f environment.yml --prune
conda activate pt_study
```

For notebook use, register the environment as a kernel:

```powershell
python -m ipykernel install --user --name pt_study --display-name "pt_study"
```

Core geospatial dependencies include GeoPandas, Pyogrio, Shapely, Fiona, OSMnx, pandas, NumPy, SciPy, Folium, Mercantile, r5py, and pytest. Travel-time routing requires JDK 21; `environment.yml` installs the matching OpenJDK package.

## Running The Urban-Area Workflow

Run one configured city:

```powershell
conda activate pt_study
python calc_urban_area.py --city "Oslo, Norway" --config city_config.example.json
```

Run without writing output layers:

```powershell
python calc_urban_area.py --city "Oslo, Norway" --config city_config.example.json --no-save
```

If cached building or road layers are missing and you want the pipeline to query OSM for missing data:

```powershell
python calc_urban_area.py --city "Oslo, Norway" --config city_config.example.json --query-missing
```

The script prints the metric CRS, building mode, urban cell count, urban area, urban population share, and output paths when saving is enabled.

Run the downstream stages or the full resumable study:

```powershell
python calc_transport.py --city "Oslo, Norway" --config city_config.example.json
python calc_gtfs.py --city "Oslo, Norway" --config city_config.example.json --all-to-all
python run_study.py --cities all --stage all
python publish_outputs.py
```

Stockholm requires `TRAFIKLAB_API_KEY`. Brussels requires `STIB_GTFS_URL` and `DELIJN_GTFS_URL`; Paris requires `IDFM_GTFS_URL`. These environment variables keep credentials and provider-generated download URLs out of version control.

## Configuration

City configuration is loaded with `load_city_config()` from a JSON file. The example file contains a `cities` mapping keyed by full city name.

Important settings:

| Setting | Meaning |
| --- | --- |
| `city_name`, `city_slug` | Human-readable name and stable output slug. |
| `metric_crs` | Projected CRS used for area/length/intersection calculations. If omitted, `CityConfig` uses a city lookup or estimates UTM from the boundary. |
| `analysis_envelope_buffer_m` | Optional metric buffer around the configured boundary. Defaults to `0`. This is the current way to broaden the envelope beyond the administrative boundary. |
| `population_gpkg`, `population_layer` | Population grid input. |
| `building_gpkg`, `building_layer` | Building footprint or point-building input. Default layer name is `{slug}_buildings`. |
| `road_gpkg`, `road_layer` | Road/street input. Default layer name is `{slug}_roads`. |
| `building_mode` | `auto`, `footprint`, `point`, or `required_footprint`. |
| `centre_lon`, `centre_lat` | City-centre point: selects the study urban centre and is the travel-time destination. |
| `growth.method` | `degurba` (default, absolute thresholds) or `score` (legacy). |
| `growth.centre_density_min`, `growth.builtup_share_min`, `growth.centre_min_population` | DEGURBA urban-centre thresholds. |
| `growth.keep_other_centres` | Also keep urban centres that do not contain the city-centre point. |
| `growth.strictness`, `growth.keep_largest_component` | Legacy score-method settings. |
| `water_mask.min_land_fraction` | Cells with less land than this and no buildings are treated as water. |
| `gtfs.service_date`, `gtfs.window`, `gtfs.frequent_dep_per_hr` | Pinned representative date (outside school holidays), departure window, and frequency threshold. |
| `transport.bus_walking_distance_m` | Walking distance for the all-mode stop tier (default 500 m). |
| `refinement.enabled` | Enables edge-cell subdivision and refined boundary output. |

The default road query includes both major transport roads and local urban-fabric roads. Major transport roads are retained for diagnostics but not used as the morphology road score.

## Data Inputs

All metric calculations must run in a projected city CRS, not EPSG:4326 or EPSG:3857.

| Input | Expected geometry | Key columns / notes |
| --- | --- | --- |
| City boundary / envelope | Polygon or MultiPolygon | Must include `Name` matching `CityConfig.city_name`. Loaded from `boundaries_path` / `boundaries_layer`. |
| Population grid | Polygon or MultiPolygon | Must include `population`. H3/hex IDs may exist but are not required; the loader assigns `cell_id`. |
| Buildings | Polygon/MultiPolygon preferred; Point/MultiPoint supported as fallback | Polygon footprints drive `building_area_sqm` and `built_up_coverage`. Point-only data is lower confidence and is reported in validation warnings. |
| Roads/streets | LineString or MultiLineString | `highway` is used when present to classify local urban-fabric roads versus major transport roads. |
| Rail/stations/catchments | Points/Lines/Polygons depending on dataset | Used downstream. Catchment helpers expect station catchment polygons and, ideally, a stable `station_id`. |

Current implementation notes:

- OSM water polygons, a narrow coastline buffer, and sea polygons derived from coastline direction are cached per city. Densities use land area, near-total water cells without buildings are excluded, and published geometry is clipped to land without reducing population. Park, airport, forest, and other undeveloped-land masks are not currently implemented.
- Intersection density is **not currently calculated**. Local street density is the implemented street-fabric signal.
- Building footprints may be sourced from Microsoft Global ML Building Footprints via `get_ms_buildings.py`, OSM, or another prepared GeoPackage layer.

## Population Handling

Population is treated as an extensive count, not an intensive density.

### Clipping

`load_population_cells()` clips the input population grid to the city boundary or buffered analysis envelope. Before overlay it stores original area and original population. After clipping it calculates:

```text
population_clip_fraction = clipped_area_sqm / original_area_sqm
population = original_population * population_clip_fraction
```

Other numeric attributes are scaled only when their names look extensive, such as population, people, persons, workers, jobs, households, or counts. Attributes with names such as density, rate, ratio, share, percent, mean, median, or average are treated as intensive and left unchanged.

Zero-area, empty, invalid, or non-finite geometries are filtered or made valid before overlay where possible.

### Scoring

Urban scoring uses `population_density = population / cell_area_sqkm`, not raw population. This prevents clipped or refined cells from scoring unfairly because their geometry is smaller than a full source cell.

### Refined Edge Cells

When edge cells are subdivided, population is allocated to subcells with `allocate_population_dasymetric()`:

1. residential building footprint area, where building-use tags identify residential buildings;
2. all building footprint area;
3. building point counts;
4. area-weighted fallback.

The package records `population_allocation_method` and `population_allocation_weight` on refined cells. There is no developable-land fallback yet because no land-use mask is wired into the package.

## Urban Metrics

`add_absolute_metrics()` derives per-cell fields that do not depend on other cells; `add_urban_metrics()` adds the legacy percentile-normalised diagnostics on top.

| Field | Meaning |
| --- | --- |
| `cell_area_sqm`, `cell_area_sqkm` | Projected cell area. |
| `land_area_sqm`, `water_fraction` | Land and water share from the water mask. |
| `population_density` | Population per square kilometre of **land**. |
| `building_area_sqm` / `total_building_area_sqm` | Intersected building footprint area in the cell. |
| `built_up_coverage` / `building_coverage` | Footprint area divided by land area. |
| `local_road_density_km_per_sqkm` | Local urban-fabric road length per square kilometre. Used by edge refinement. |
| `major_road_density_km_per_sqkm` | Motorway/trunk diagnostic density. |
| `degurba_class` | `urban_centre`, `urban_cluster`, or `rural`. |
| `in_study_area` | Cell belongs to the urban centre containing the configured city-centre point. |
| `pop_norm`, `building_signal_norm`, `road_norm`, `urban_score_base` | Legacy diagnostics, robust min-max scaled between the 5th and 95th percentiles **of the analysis envelope**. Kept on analysis cells only; they are not comparable across cities. |

## Urban Classification (DEGURBA-style)

`growth.method = "degurba"` (the default) adapts the Degree of Urbanisation to the Kontur H3 resolution-8 cells (`urban_area/degurba.py`):

1. A cell qualifies for an urban centre when its land population density is at least `centre_density_min` (1,500/km²), or its footprint built-up share is at least `builtup_share_min` (0.20).
2. Edge-adjacent qualifying cells form clusters. Clusters with at least `centre_min_population` (50,000) residents become urban centres.
3. Gaps are filled iteratively: a non-water cell with at least `gap_fill_min_neighbors` (4 of 6) neighbours in a centre joins it.
4. Enclosed non-water holes up to `max_hole_area_sqkm` (15 km²) are filled.
5. Other cells of at least `cluster_density_min` (300/km²) in contiguous groups of at least `cluster_min_population` (5,000) become urban clusters.
6. The study area is the urban centre containing `centre_lon`/`centre_lat` (all centres if `keep_other_centres`).

The thresholds are absolute, so the same rule applies in every city regardless of envelope size. The GHSL built-up rule uses 50% built-up surface, but footprint coverage measures much less than built-up surface, so the 0.20 default is a calibration choice. H3 r8 cells (about 0.74 km²) are smaller than the 1 km² DEGURBA grid, which makes density thresholds slightly stricter. Validation reruns the classification at 1,200, 1,500, and 2,000/km².

`growth.method = "score"` keeps the legacy percentile-normalised region growing (below) for regression comparison.

## Building Data Handling

`aggregate_buildings_to_cells()` prefers true footprint polygons. In `building_mode = "auto"`, polygons are used if present; point data is used only when no polygons are present. `required_footprint` and `footprint` fail when no polygon geometries are available.

Output columns include:

- `building_area_sqm`;
- `total_building_area_sqm`;
- `building_count`;
- `building_point_count`;
- `building_point_density_per_sqkm`;
- `building_signal_type`;
- `building_signal_confidence`;
- `built_up_coverage`.

Validation records geometry type counts. Point-only data emits warnings that footprint coverage is unavailable and point density is a lower-confidence fallback. Mixed polygon/point data is allowed, with polygons preferred.

## Road And Street Handling

Roads are classified by OSM `highway` value:

- major transport diagnostics: `motorway`, `trunk`, `motorway_link`, `trunk_link`;
- local urban-fabric roads: `primary`, `secondary`, `tertiary`, `unclassified`, `residential`, `living_street`, `primary_link`, `secondary_link`, `tertiary_link`, `service`.

All roads remain available in `road_density_km_per_sqkm` and length diagnostics, but only local urban-fabric roads feed `road_norm`. Road-only cells cannot seed the urban area and motorways/trunks cannot independently create built-up urban cells.

## Legacy Score Method: Seed Selection

`growth.py` defines three strictness presets: `strict`, `medium`, and `loose`. Each preset controls:

- seed quantile (`seed_q`);
- minimum balanced score (`score_min`);
- minimum population/building/road normalised values for combination rules;
- strong population and strong building thresholds;
- neighbour relaxation.

Seed clusters are selected from high `urban_score_base` cells above the configured quantile, but cells must have positive score and some population or building evidence. If no valid seeds exist, the algorithm returns an empty urban area rather than treating road-only or zero-evidence cells as urban.

## Legacy Score Method: Region Growth

`run_growing_method()` performs multi-seed breadth-first growth over an adjacency graph.

Adjacency is built by `build_adjacency_graph()` using shared boundary length by default (`mode="edge"`), which avoids point-only corner contacts. The default minimum shared boundary is 1 metre.

A neighbouring cell can be added if it satisfies one of these pathways:

- strong population density;
- strong built-form/building signal;
- a balanced moderate combination of population density, built-up coverage, and local street fabric.

This lets low-resident but clearly built-up places, such as CBDs, industrial areas, universities, hospitals, airports, or employment zones, qualify when building evidence is strong. At the same time, road false positives are controlled because the supporting road signal is local-street density and road-only cells are not valid seeds.

The `water_guard` option blocks growth through cells with negligible population and building signal. It is a guardrail, not a substitute for a proper water/land-use mask.

### Water handling

OSM holds the sea only as coastline lines (land on the left), so `sea_polygons_from_coastline()` cuts the envelope along the coastline and votes each piece sea or land from probe points either side of the coastline; truncated coastlines fall back to land. Only polygons carrying a water tag count as water: closed coastline rings are islands, and land-tagged member ways of water relations (lake islands) stay land. The cached layer is `water_mask_v3_{slug}`.

The water mask annotates `land_area_sqm` on every cell before metrics are calculated, so densities and coverage are measured on land. A cell is flagged `is_water` only when less than `min_land_fraction` (10%) of it is land and it has under 100 m² of buildings. Published cells are clipped to land, but **population and building area are not scaled by the clip**, because residents and buildings are already on the land part of the cell. The earlier rule (hard-excluding any cell at least 50% water and area-scaling population on clip) removed more than 13,000 Oslo residents on the waterfront.

After growth, enclosed holes with some population or building evidence can be filled. Component handling then either:

- keeps only the largest component, when `keep_largest_component = true`; or
- keeps all components meeting configured minimum population, area, or built-up-area thresholds.

There is no explicit functional-connection threshold yet.

## Edge Refinement

When refinement is enabled, boundary cells are subdivided into a rectangular grid inside each edge cell. The default `subdivision_factor = 4` creates up to 16 subcells per edge cell.

The refinement step:

1. identifies urban cells adjacent to non-urban cells;
2. subdivides those edge cells;
3. allocates parent-cell population to subcells with the dasymetric hierarchy described above;
4. aggregates buildings and roads to subcells;
5. keeps subcells with local presence and either enough building area/points or enough local street density;
6. recombines non-edge parent cells with qualifying refined subcells;
7. reapplies component retention and recomputes urban metrics.

Refined cells are not H3 cells. They preserve `parent_cell_id`, set `is_refined = true`, set `h3` to missing where applicable, and carry `population_allocation_method`.

## Station And Catchment Logic

The urban polygon should not be used as a simple point-in-polygon station filter.

Current downstream helpers in `catchment.py` provide:

- `population_within_polygon()`: area-weighted, building-weighted, auto, or centroid-based population within a polygon;
- `population_within_catchments()`: dissolves catchment polygons by default before calculating total covered population, avoiding double counting where station catchments overlap;
- `filter_stations_by_catchment_population()`: keeps stations if their walking catchment covers at least a configured urban population or, when an all-population denominator is provided, a configured urban share.

Typical station-filtering logic should be:

1. build or load walking catchment polygons for stations;
2. intersect catchments with the urban population cells;
3. keep stations with enough covered urban population, or enough urban share of their total catchment population;
4. dissolve all kept catchments before calculating total population covered by the network.

## Outputs

### Revised Oslo run (2026-10-18)

Oslo has been re-run with DEGURBA classification, land-area densities with sea polygons, GTFS-built stations, per-direction frequency, and the all-mode tier. The other five cities still hold the earlier score-method results (flagged `score_legacy` in `city_summary.csv`) and must be re-run before comparison.

| Oslo measure | Earlier (2026-08-09) | Revised |
| --- | ---: | ---: |
| Urban area (km²) | 244.72 | 191.30 |
| Urban population | 792,054 | 714,052 |
| Rail, metro, and tram stations | 305 | 205 (99 metro, 72 tram, 34 rail) |
| Within 15 min walk of a station | 58.59%* | 63.84% |
| Within 15 min of a frequent station (≥6/h busiest direction) | 42.86%* | 53.49% |
| Within 500 m walk of any stop | – | 72.16% |
| Frequent service of any mode | – | 70.79% |
| Median PT time to centre / within 45 min | – | 35 min / 82.9% |
| Urban residents reachable in 30 / 45 min (average resident) | – | 9.0% / 33.9% |

\* Computed on the pre-water-mask urban population (804,250), with all tram stops at zero departures.

Runtimes on cached inputs: urban 1.3 min, transport 8.8 min (the 211 catchments take seconds with scipy), travel time 9.1 min including the one-off clipped-feed preparation and the all-to-all matrix.

### Water-masked six-city run (2026-08-09, superseded for Oslo)

The final cached urban pass completed all six cities in 863.2 seconds (14m 23s). Timings exclude the one-off Microsoft-footprint and Geofabrik PBF downloads.

| City | Runtime | Urban area (km²) | Urban population | Urban share | Published urban cells |
| --- | ---: | ---: | ---: | ---: | ---: |
| Stockholm | 108.7 s | 317.68 | 1,362,317 | 63.70% | 4,492 |
| Copenhagen | 136.6 s | 260.71 | 1,490,353 | 59.50% | 2,550 |
| Helsinki | 174.5 s | 311.84 | 815,055 | 79.39% | 4,386 |
| Brussels | 38.2 s | 173.13 | 1,314,822 | 74.94% | 2,311 |
| Paris | 311.1 s | 1,058.01 | 8,608,895 | 73.41% | 10,007 |
| Oslo | 94.1 s | 244.72 | 792,054 | 82.26% | 3,431 |

These are the water-masked results. The earlier Oslo no-water regression gate remains 251.10 km², 804,251 people, and 83.5%; it is retained as a code-performance regression, not the current published result.

The principal working and published files are:

| Purpose | Path |
| --- | --- |
| Six-city urban cells and boundaries | `data/urban_areas.gpkg` |
| Per-city OSM water masks | `data/water_masks.gpkg` |
| Per-city normalized road linework | `data/road_networks.gpkg` |
| Exact final urban timings | `data/logs/timing_summary.csv` |
| Per-city run state, provenance, and validation | `data/provenance/` |
| ArcGIS-facing consolidated layers (EPSG:3035) | `../Output/pt_study.gpkg` |
| Six-city comparison table | `../Output/city_summary.csv` |

For Oslo specifically, the final cached urban stage takes about 1m34s. The first uncached transport/catchment build took about 2h08m including its checkpoint resume; the cached GTFS frequency stage took about 3m31s. Oslo's weighted 15-minute coverage is 58.59%, and frequent-service coverage is 42.86%.

When `write_outputs = true`, `save_pipeline_outputs()` writes:

| Output | Location / naming |
| --- | --- |
| Enriched analysis cells | `population_gpkg` layer `population_density_with_buildings_roads_{slug}` |
| Urban cells | `urban_areas.gpkg` layer `urban_cells_{slug}_{method}` |
| Urban cells GeoJSON | `data/urban_cells_{slug}_{method}.geojson` |
| Dissolved urban boundary | `urban_areas.gpkg` layer `urban_area_{slug}_{method}` |
| Provenance JSON | `data/provenance/{slug}_{method}_provenance.json` |
| Validation JSON | `data/provenance/{slug}_{method}_validation.json` |

Transport stages additionally write city-suffixed rail/station layers, 15-minute isochrones, coverage cells, GTFS/travel-time Parquet files, and stage-specific provenance/validation JSON. `publish_outputs.py` consolidates the final six-city contract into `Output/pt_study.gpkg`.

`method` is `growing_refined` when refinement is enabled and `growing` otherwise. The boundary output is a dissolved union of selected cells, even though the function name is `hull_from_cells()`.

The provenance JSON records city name, CRS, building mode, source geometry types, config, road signal semantics, output layer names, and the fact that the output is a `morphological_built_up_area`.

## Validation And Quality Checks

`build_validation_report()` records:

- schema/contract checks for population cells, buildings, roads, and analysis cells;
- total analysed population;
- urban population and urban population share;
- analysis and urban area in square kilometres;
- component count and component areas;
- hole count and hole area;
- distributions for building coverage, built-up coverage, population density, building point density, all-road density, local-road density, major-road density, and base urban score;
- road length aggregation check;
- sensitivity summaries for strict, medium, and loose growth settings.

Useful tests and checks:

```powershell
conda activate pt_study
pytest tests
```

The synthetic test suite currently covers:

- area-weighted population clipping;
- population-density scoring;
- per-cell road density;
- major-road false-positive prevention;
- road-only cells not seeding urban areas;
- point-building fallback warnings and confidence;
- high-built-up/low-population growth;
- edge adjacency excluding point-only contacts;
- dasymetric allocation order;
- preservation of population allocation metadata after refinement;
- dissolved catchment coverage avoiding double counting;
- station filtering with catchment population and share denominators.

For research validation, compare outputs against external references where available, such as GHSL/DEGURBA-style built-up classifications, OECD Functional Urban Areas, national urban-area datasets, or manually reviewed city boundaries. `benchmark_ghsl.py` automates the GHSL comparison once the benchmark layer is supplied; the other comparisons remain manual.

## Script And Notebook Notes

- `calc_urban_area.py` is the preferred repeatable runner.
- `calc_urban_area.ipynb` starts with a thin call to the package and then keeps older exploratory mapping/diagnostic cells. Treat the package as the source of truth.
- `road-density.py` is useful when you only need road aggregation using the same CRS/clipping assumptions.
- `get_ms_buildings.py` uses WGS84 tile bounds for Microsoft building downloads, filters by the city AOI, then calculates footprint area in the city metric CRS.
- `model_census.ipynb`, `get_metro_data.ipynb`, and `osm_experiments.ipynb` are retained under `../Archive/notebooks/` as historical references.
- `transport/` is the source of truth for OSM rail/station, isochrone, GTFS, and r5py analysis; the regenerated Oslo transport gate was completed on 2026-08-09.

## Data Directory

The repository expects processed data under `./data/` by default:

- `population_density.gpkg`;
- `building_footprints.gpkg`;
- `road_networks.gpkg`;
- `water_masks.gpkg`;
- `urban_areas.gpkg`;
- `rail_lines.gpkg`;
- `station_isochrones.gpkg`;
- `walking_networks.gpkg`;
- `data/osm/{slug}.osm.pbf`;
- `data/logs/timing_summary.csv`;
- `data/provenance/`.

The default city boundary path in `CityConfig` is:

```text
D:/Public Transport Study/Input Data/Boundaries/Boundaries.gpkg
```

Override paths in `city_config.example.json` or by constructing `CityConfig` directly if your workspace differs.

## Limitations

- Building data completeness varies by city. Footprints are preferred; point-only fallback is lower confidence and should be treated cautiously.
- OSM road tagging quality varies. Major roads are diagnostic only, but local street density can still be sparse or inconsistent in some regions.
- The water mask derives the sea from coastline direction inside each envelope; where a coastline is truncated by the extract, that stretch of sea falls back to land. Other undeveloped-land masks remain future work.
- Intersection density is not implemented yet.
- Edge-cell population allocation is best when residential building-use tags or complete footprint data exist. Without building evidence, refined cells fall back to area weighting.
- The morphological built-up area is not a commuting or service region. Use catchment logic, and eventually FUA/commuting data, for functional public transport service interpretation.
- Administrative boundaries can be too narrow or too broad. `analysis_envelope_buffer_m` can broaden the search envelope, but it is a simple buffer rather than a learned metro envelope.
- Benchmark validation against GHSL/DEGURBA/OECD FUA or official local boundaries is recommended before comparing cities globally.

## Visualization And Mapping

Final cartographic presentation is still expected to happen outside the package, for example in ArcGIS Pro or Folium notebooks, using the GeoPackage/GeoJSON outputs. Keep analytical semantics in the package and use notebooks/maps for review, diagnostics, and presentation.
