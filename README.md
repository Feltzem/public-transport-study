# Public Transport Study

This repository processes urban, population, street, building, and public transport data for cities around the world. The current urban-area workflow first identifies a **morphological built-up urban area**: a contiguous area of urban fabric based on population density, building evidence, and local street fabric. That area is then used as a defensible population denominator and spatial frame for public transport coverage analysis.

The built-up area is not the same thing as a public transport service area. Stations and catchments should be evaluated with walking-catchment population logic, not by simply checking whether a station point lies inside the built-up polygon.

## Project Overview

The workflow has three main parts:

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

3. **Public transport catchment analysis**
   - Use the morphological built-up area as the denominator and urban population mask.
   - Keep stations based on walking-catchment population/intersection criteria.
   - Dissolve station catchments before calculating total covered population to avoid double counting.

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
| `calc_urban_area.ipynb` | Thin notebook entry point plus older exploratory diagnostics. The top cells call `run_urban_area_pipeline`; package code is the source of truth. |
| `urban_area/` | Reusable implementation for config, I/O, scoring, growth, refinement, validation, and catchment helpers. |
| `city_config.example.json` | Example city-specific settings for Oslo and Stockholm. |
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

Core geospatial dependencies include GeoPandas, Shapely, Fiona, OSMnx, pandas, NumPy, SciPy, Folium, Mercantile, and pytest.

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
| `growth.strictness` | `strict`, `medium`, or `loose` parameter set. |
| `growth.keep_largest_component` | If `true`, keep only the largest component. If `false`, retain components meeting configured population, area, or built-up-area thresholds. |
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

- Explicit water, land-use, park, airport, forest, or undeveloped-land masks are **not currently implemented** in the package. The growth `water_guard` prevents expansion through cells with negligible population and building signal, but it is not a true water/land-use mask.
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

`add_urban_metrics()` derives the main per-cell scoring fields:

| Field | Meaning |
| --- | --- |
| `cell_area_sqm`, `cell_area_sqkm` | Projected cell area. |
| `population_density` | Population per square kilometre. Used for `pop_norm`. |
| `building_area_sqm` / `total_building_area_sqm` | Intersected building footprint area in the cell. |
| `built_up_coverage` / `building_coverage` | Building footprint area divided by cell area. |
| `building_count` | Count of unique footprint polygons or building points, depending on mode. |
| `building_point_count` | Point-building count for point fallback mode. |
| `building_point_density_per_sqkm` | Point fallback density. |
| `building_signal_type` | `footprint_area`, `point_fallback`, or `none`. |
| `building_signal_confidence` | `1.0` for footprints, `0.5` for point fallback, `0.0` for no signal. |
| `road_density_km_per_sqkm` | All road length per square kilometre. Diagnostic. |
| `local_road_density_km_per_sqkm` | Local urban-fabric road length per square kilometre. Used for `road_norm`. |
| `major_road_density_km_per_sqkm` | Motorway/trunk diagnostic density. Not used as the morphology road score. |

Normalised fields are robust min-max scaled between the 5th and 95th percentiles:

- `pop_norm` from `population_density`;
- `bldg_cov_norm` from `built_up_coverage`;
- `bldg_point_norm` from `building_point_density_per_sqkm`;
- `road_norm` from `local_road_density_km_per_sqkm` when available.

The base score is:

```text
urban_score_base = 0.52 * pop_norm
                 + 0.43 * building_signal_norm
                 + 0.05 * road_norm
```

For point-building fallback, `building_signal_norm` is downweighted by `building_signal_confidence`.

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

## Scoring And Seed Selection

`growth.py` defines three strictness presets: `strict`, `medium`, and `loose`. Each preset controls:

- seed quantile (`seed_q`);
- minimum balanced score (`score_min`);
- minimum population/building/road normalised values for combination rules;
- strong population and strong building thresholds;
- neighbour relaxation.

Seed clusters are selected from high `urban_score_base` cells above the configured quantile, but cells must have positive score and some population or building evidence. If no valid seeds exist, the algorithm returns an empty urban area rather than treating road-only or zero-evidence cells as urban.

## Region Growth

`run_growing_method()` performs multi-seed breadth-first growth over an adjacency graph.

Adjacency is built by `build_adjacency_graph()` using shared boundary length by default (`mode="edge"`), which avoids point-only corner contacts. The default minimum shared boundary is 1 metre.

A neighbouring cell can be added if it satisfies one of these pathways:

- strong population density;
- strong built-form/building signal;
- a balanced moderate combination of population density, built-up coverage, and local street fabric.

This lets low-resident but clearly built-up places, such as CBDs, industrial areas, universities, hospitals, airports, or employment zones, qualify when building evidence is strong. At the same time, road false positives are controlled because the supporting road signal is local-street density and road-only cells are not valid seeds.

The `water_guard` option blocks growth through cells with negligible population and building signal. It is a guardrail, not a substitute for a proper water/land-use mask.

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

When `write_outputs = true`, `save_pipeline_outputs()` writes:

| Output | Location / naming |
| --- | --- |
| Enriched analysis cells | `population_gpkg` layer `population_density_with_buildings_roads_{slug}` |
| Urban cells | `urban_areas.gpkg` layer `urban_cells_{slug}_{method}` |
| Urban cells GeoJSON | `data/urban_cells_{slug}_{method}.geojson` |
| Dissolved urban boundary | `urban_areas.gpkg` layer `urban_area_{slug}_{method}` |
| Provenance JSON | `data/provenance/{slug}_{method}_provenance.json` |
| Validation JSON | `data/provenance/{slug}_{method}_validation.json` |

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

For research validation, compare outputs against external references where available, such as GHSL/DEGURBA-style built-up classifications, OECD Functional Urban Areas, national urban-area datasets, or manually reviewed city boundaries. Those comparisons are not automated in the package yet.

## Script And Notebook Notes

- `calc_urban_area.py` is the preferred repeatable runner.
- `calc_urban_area.ipynb` starts with a thin call to the package and then keeps older exploratory mapping/diagnostic cells. Treat the package as the source of truth.
- `road-density.py` is useful when you only need road aggregation using the same CRS/clipping assumptions.
- `get_ms_buildings.py` uses WGS84 tile bounds for Microsoft building downloads, filters by the city AOI, then calculates footprint area in the city metric CRS.
- `model_census.ipynb` is exploratory work toward commuting/FUA-style analysis and may be relevant for future functional/service area definitions.
- `get_metro_data.ipynb` handles OSM rail/station data collection and downstream transport network inputs.

## Data Directory

The repository expects processed data under `./data/` by default:

- `population_density.gpkg`;
- `building_footprints.gpkg`;
- `road_networks.gpkg`;
- `urban_areas.gpkg`;
- `rail_lines.gpkg`;
- `station_isochrones.gpkg`;
- `walking_networks.gpkg`;
- `data/provenance/`.

The default city boundary path in `CityConfig` is:

```text
D:/Public Transport Study/Input Data/Boundaries/Boundaries.gpkg
```

Override paths in `city_config.example.json` or by constructing `CityConfig` directly if your workspace differs.

## Limitations

- Building data completeness varies by city. Footprints are preferred; point-only fallback is lower confidence and should be treated cautiously.
- OSM road tagging quality varies. Major roads are diagnostic only, but local street density can still be sparse or inconsistent in some regions.
- No explicit water, land-use, park, forest, airport, or undeveloped masks are currently applied. The implemented `water_guard` only blocks growth through cells with negligible population and building signal.
- Intersection density is not implemented yet.
- Edge-cell population allocation is best when residential building-use tags or complete footprint data exist. Without building evidence, refined cells fall back to area weighting.
- The morphological built-up area is not a commuting or service region. Use catchment logic, and eventually FUA/commuting data, for functional public transport service interpretation.
- Administrative boundaries can be too narrow or too broad. `analysis_envelope_buffer_m` can broaden the search envelope, but it is a simple buffer rather than a learned metro envelope.
- Benchmark validation against GHSL/DEGURBA/OECD FUA or official local boundaries is recommended before comparing cities globally.

## Visualization And Mapping

Final cartographic presentation is still expected to happen outside the package, for example in ArcGIS Pro or Folium notebooks, using the GeoPackage/GeoJSON outputs. Keep analytical semantics in the package and use notebooks/maps for review, diagnostics, and presentation.
