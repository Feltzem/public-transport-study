import pytest

gpd = pytest.importorskip("geopandas")
pytest.importorskip("shapely")

from shapely.geometry import LineString, Point, Polygon

from urban_area.buildings import aggregate_buildings_to_cells
from urban_area.catchment import (
    filter_stations_by_catchment_population,
    population_within_catchments,
    population_within_polygon,
)
from urban_area.config import CityConfig
from urban_area.degurba import classify_degurba, select_study_centres
from urban_area.growth import run_growing_method
from urban_area.io import load_population_cells
from urban_area.refinement import allocate_population_dasymetric, refine_urban_edges
from urban_area.geometry import build_adjacency_graph, vectorized_overlay_measure
from urban_area.metrics import add_urban_metrics
from urban_area.roads import aggregate_roads_to_cells, normalize_roads
from urban_area.schemas import validate_buildings
from urban_area.water import sea_polygons_from_coastline, water_features_to_mask


def _cells():
    return gpd.GeoDataFrame(
        {
            "cell_id": [1, 2],
            "population": [100, 100],
            "h3": ["a", "b"],
            "Name": ["Test City", "Test City"],
            "geometry": [
                Polygon([(0, 0), (10, 0), (10, 10), (0, 10)]),
                Polygon([(10, 0), (30, 0), (30, 10), (10, 10)]),
            ],
        },
        crs="EPSG:32633",
    )


def test_point_buildings_are_supported_as_density_signal():
    buildings = gpd.GeoDataFrame(
        {"geometry": [Point(1, 1), Point(2, 2), Point(20, 5)]},
        crs="EPSG:32633",
    )

    aggregated = aggregate_buildings_to_cells(buildings, _cells(), building_mode="auto")
    assert aggregated.mode == "point"
    assert aggregated.warnings
    assert aggregated.cells["total_building_area_sqm"].sum() == 0
    assert aggregated.cells["building_point_count"].tolist() == [2, 1]
    assert set(aggregated.cells["building_signal_type"]) == {"point_fallback"}

    scored = add_urban_metrics(aggregated.cells, building_mode=aggregated.mode)
    assert "building_signal_norm" in scored.columns
    assert scored["building_signal_norm"].max() > 0
    assert scored["building_signal_norm"].max() <= 0.5


def test_building_contract_warns_for_point_only_cache():
    buildings = gpd.GeoDataFrame({"geometry": [Point(1, 1)]}, crs="EPSG:4326")
    report = validate_buildings(buildings, expected_mode="auto")
    assert report.ok
    assert any("point geometries" in warning for warning in report.warnings)


def test_road_density_uses_each_cell_area_not_first_cell_area():
    roads = gpd.GeoDataFrame(
        {"geometry": [LineString([(0, 5), (10, 5)]), LineString([(10, 5), (30, 5)])]},
        crs="EPSG:32633",
    )

    result = aggregate_roads_to_cells(roads, _cells())
    densities = result.set_index("cell_id")["road_density_km_per_sqkm"]
    assert densities.loc[1] == pytest.approx(100.0)
    assert densities.loc[2] == pytest.approx(100.0)


def test_normalize_roads_converts_closed_osm_highways_to_linework():
    polygon_road = gpd.GeoDataFrame(
        {"highway": ["residential"]},
        geometry=[Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])],
        crs="EPSG:3857",
    )

    normalized = normalize_roads(polygon_road, "EPSG:3857")

    assert normalized.geom_type.tolist() == ["LineString"]
    assert normalized["length_m"].iloc[0] == pytest.approx(40.0)


def test_water_mask_handles_inland_city_without_coastline_features():
    water = gpd.GeoDataFrame(
        {"natural": ["water"]},
        geometry=[Polygon([(1, 1), (4, 1), (4, 4), (1, 4)])],
        crs="EPSG:32633",
    )
    boundary = gpd.GeoDataFrame(
        geometry=[Polygon([(0, 0), (5, 0), (5, 5), (0, 5)])],
        crs="EPSG:32633",
    )

    mask = water_features_to_mask(water, boundary, "EPSG:32633")

    assert len(mask) == 1
    assert mask["water_source"].tolist() == ["osm_water_polygon"]


def test_water_mask_keeps_islands_and_untagged_polygons_as_land():
    # A closed coastline ring (an island) and a lake-island member way tagged
    # natural=wood are exported as polygons but must not become water.
    water = gpd.GeoDataFrame(
        {"natural": ["water", "coastline", "wood"], "landuse": [None, None, None]},
        geometry=[
            Polygon([(1, 1), (2, 1), (2, 2), (1, 2)]),
            Polygon([(3, 3), (4, 3), (4, 4), (3, 4)]),
            Polygon([(1, 3), (2, 3), (2, 4), (1, 4)]),
        ],
        crs="EPSG:32633",
    )
    boundary = gpd.GeoDataFrame(geometry=[Polygon([(0, 0), (5, 0), (5, 5), (0, 5)])], crs="EPSG:32633")

    mask = water_features_to_mask(water, boundary, "EPSG:32633", coastline_buffer_m=0)

    assert mask["water_source"].tolist() == ["osm_water_polygon"]
    assert mask.geometry.iloc[0].area == pytest.approx(1.0)


def test_water_mask_accepts_reservoir_landuse_polygon():
    water = gpd.GeoDataFrame(
        {"landuse": ["reservoir"]},
        geometry=[Polygon([(1, 1), (2, 1), (2, 2), (1, 2)])],
        crs="EPSG:32633",
    )
    boundary = gpd.GeoDataFrame(geometry=[Polygon([(0, 0), (5, 0), (5, 5), (0, 5)])], crs="EPSG:32633")

    mask = water_features_to_mask(water, boundary, "EPSG:32633")

    assert mask["water_source"].tolist() == ["osm_water_polygon"]


def test_population_cells_are_area_weighted_when_clipped(tmp_path):
    population_path = tmp_path / "population.gpkg"
    population = gpd.GeoDataFrame(
        {"population": [100.0], "workers_count": [20.0], "density_value": [1000.0]},
        geometry=[Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])],
        crs="EPSG:32633",
    )
    population.to_file(population_path, layer="population", driver="GPKG")
    boundary = gpd.GeoDataFrame(
        {"Name": ["Test City"]},
        geometry=[Polygon([(0, 0), (5, 0), (5, 10), (0, 10)])],
        crs="EPSG:32633",
    )

    config = CityConfig(city_name="Test City", population_gpkg=population_path, population_layer="population")
    clipped = load_population_cells(config, boundary, "EPSG:32633")

    assert clipped["population"].sum() == pytest.approx(50.0)
    assert clipped["workers_count"].sum() == pytest.approx(10.0)
    assert clipped["density_value"].iloc[0] == pytest.approx(1000.0)
    assert clipped["population_clip_fraction"].iloc[0] == pytest.approx(0.5)


def test_population_density_drives_population_score():
    cells = gpd.GeoDataFrame(
        {
            "cell_id": [1, 2],
            "population": [100.0, 100.0],
            "building_area_sqm": [0.0, 0.0],
            "total_building_area_sqm": [0.0, 0.0],
            "local_road_density_km_per_sqkm": [0.0, 0.0],
            "geometry": [
                Polygon([(0, 0), (10, 0), (10, 10), (0, 10)]),
                Polygon([(10, 0), (30, 0), (30, 10), (10, 10)]),
            ],
        },
        crs="EPSG:32633",
    )

    scored = add_urban_metrics(cells, building_mode="footprint")
    by_id = scored.set_index("cell_id")

    assert by_id.loc[1, "population_density"] > by_id.loc[2, "population_density"]
    assert by_id.loc[1, "pop_norm"] > by_id.loc[2, "pop_norm"]


def test_major_roads_are_diagnostic_not_urban_fabric_signal():
    cells = _cells()
    roads = gpd.GeoDataFrame(
        {
            "highway": ["motorway", "residential"],
            "geometry": [LineString([(0, 5), (10, 5)]), LineString([(10, 5), (30, 5)])],
        },
        crs="EPSG:32633",
    )

    result = aggregate_roads_to_cells(roads, cells)
    by_id = result.set_index("cell_id")

    assert by_id.loc[1, "road_density_km_per_sqkm"] > 0
    assert by_id.loc[1, "major_road_density_km_per_sqkm"] > 0
    assert by_id.loc[1, "local_road_density_km_per_sqkm"] == pytest.approx(0.0)
    assert by_id.loc[2, "local_road_density_km_per_sqkm"] > 0


def test_road_only_cells_do_not_seed_urban_area():
    cells = gpd.GeoDataFrame(
        {
            "cell_id": [1, 2],
            "population": [0.0, 0.0],
            "building_area_sqm": [0.0, 0.0],
            "total_building_area_sqm": [0.0, 0.0],
            "local_road_density_km_per_sqkm": [10.0, 0.0],
            "geometry": [
                Polygon([(0, 0), (100, 0), (100, 100), (0, 100)]),
                Polygon([(100, 0), (200, 0), (200, 100), (100, 100)]),
            ],
        },
        crs="EPSG:32633",
    )
    scored = add_urban_metrics(cells, building_mode="footprint")

    urban = run_growing_method(scored, urban_strictness="loose", keep_largest_component=True)

    assert urban.empty


def test_growth_allows_high_built_low_population_neighbor():
    cells = gpd.GeoDataFrame(
        {
            "cell_id": [1, 2, 3],
            "population": [2000.0, 0.0, 0.0],
            "building_area_sqm": [300000.0, 450000.0, 0.0],
            "total_building_area_sqm": [300000.0, 450000.0, 0.0],
            "local_road_density_km_per_sqkm": [4.0, 4.0, 0.0],
            "geometry": [
                Polygon([(0, 0), (1000, 0), (1000, 1000), (0, 1000)]),
                Polygon([(1000, 0), (2000, 0), (2000, 1000), (1000, 1000)]),
                Polygon([(2000, 0), (3000, 0), (3000, 1000), (2000, 1000)]),
            ],
        },
        crs="EPSG:32633",
    )
    scored = add_urban_metrics(cells, building_mode="footprint")

    urban = run_growing_method(scored, urban_strictness="medium", keep_largest_component=True)

    assert 2 in set(urban["cell_id"])
    assert 3 not in set(urban["cell_id"])


def test_dasymetric_population_prefers_residential_footprints_then_area_fallback():
    parent = gpd.GeoDataFrame(
        {
            "cell_id": [10],
            "population": [100.0],
            "geometry": [Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])],
        },
        crs="EPSG:32633",
    )
    subcells = gpd.GeoDataFrame(
        {
            "cell_id": [1, 2],
            "parent_cell_id": [10, 10],
            "geometry": [
                Polygon([(0, 0), (5, 0), (5, 10), (0, 10)]),
                Polygon([(5, 0), (10, 0), (10, 10), (5, 10)]),
            ],
        },
        crs="EPSG:32633",
    )
    buildings = gpd.GeoDataFrame(
        {
            "building": ["residential", "industrial"],
            "geometry": [
                Polygon([(1, 1), (4, 1), (4, 9), (1, 9)]),
                Polygon([(6, 1), (9, 1), (9, 9), (6, 9)]),
            ],
        },
        crs="EPSG:32633",
    )

    allocated = allocate_population_dasymetric(subcells, parent, buildings).set_index("cell_id")
    assert allocated.loc[1, "population"] == pytest.approx(100.0)
    assert allocated.loc[2, "population"] == pytest.approx(0.0)
    assert allocated.loc[1, "population_allocation_method"] == "residential_building_footprints"

    fallback = allocate_population_dasymetric(subcells, parent, buildings.iloc[0:0]).set_index("cell_id")
    assert fallback.loc[1, "population"] == pytest.approx(50.0)
    assert fallback.loc[2, "population"] == pytest.approx(50.0)
    assert set(fallback["population_allocation_method"]) == {"area_weighted_fallback"}


def test_refinement_preserves_population_allocation_method():
    parent = gpd.GeoDataFrame(
        {
            "cell_id": [1, 2],
            "population": [100.0, 0.0],
            "building_area_sqm": [5000.0, 0.0],
            "total_building_area_sqm": [5000.0, 0.0],
            "building_count": [1, 0],
            "building_point_count": [0, 0],
            "building_signal_type": ["footprint_area", "footprint_area"],
            "built_up_coverage": [0.05, 0.0],
            "local_road_density_km_per_sqkm": [1.0, 0.0],
            "road_density_km_per_sqkm": [1.0, 0.0],
            "geometry": [
                Polygon([(0, 0), (100, 0), (100, 100), (0, 100)]),
                Polygon([(100, 0), (200, 0), (200, 100), (100, 100)]),
            ],
        },
        crs="EPSG:32633",
    )
    urban = add_urban_metrics(parent.iloc[[0]].copy(), building_mode="footprint")
    analysis = add_urban_metrics(parent.copy(), building_mode="footprint")
    buildings = gpd.GeoDataFrame(
        {"building": ["residential"], "geometry": [Polygon([(10, 10), (40, 10), (40, 40), (10, 40)])]},
        crs="EPSG:32633",
    )
    roads = gpd.GeoDataFrame({"geometry": []}, geometry="geometry", crs="EPSG:32633")

    refined = refine_urban_edges(
        urban,
        analysis,
        buildings,
        roads,
        building_mode="footprint",
        subdivision_factor=2,
        min_building_area_sqm=1,
        min_road_density_km_per_sqkm=999,
    )

    assert "population_allocation_method" in refined.columns
    assert "residential_building_footprints" in set(refined["population_allocation_method"])


def test_edge_adjacency_excludes_point_only_contacts():
    cells = gpd.GeoDataFrame(
        {
            "cell_id": [1, 2],
            "geometry": [
                Polygon([(0, 0), (1, 0), (1, 1), (0, 1)]),
                Polygon([(1, 1), (2, 1), (2, 2), (1, 2)]),
            ],
        },
        crs="EPSG:32633",
    )

    assert build_adjacency_graph(cells, mode="edge") == {}
    assert build_adjacency_graph(cells, mode="touches") == {1: {2}, 2: {1}}


def test_vectorized_overlay_measure_matches_direct_intersections():
    zones = _cells()[["cell_id", "geometry"]]
    features = gpd.GeoDataFrame(
        {
            "feature_id": [10, 11],
            "geometry": [
                Polygon([(5, 0), (15, 0), (15, 10), (5, 10)]),
                Polygon([(20, 0), (25, 0), (25, 10), (20, 10)]),
            ],
        },
        crs=zones.crs,
    )

    measured = vectorized_overlay_measure(
        features,
        zones,
        feature_id_col="feature_id",
        zone_id_col="cell_id",
        measure="area",
    )
    actual = measured.groupby("cell_id")["intersection_measure"].sum().to_dict()
    expected = {
        cell_id: sum(feature.intersection(zone).area for feature in features.geometry)
        for cell_id, zone in zip(zones["cell_id"], zones.geometry)
    }

    assert actual == pytest.approx(expected)


def test_area_weighted_catchment_population():
    cells = gpd.GeoDataFrame(
        {"population": [100], "geometry": [Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])]},
        crs="EPSG:32633",
    )
    catchment = Polygon([(0, 0), (5, 0), (5, 10), (0, 10)])

    population, covered = population_within_polygon(cells, catchment, method="area_weighted")
    assert population == pytest.approx(50)
    assert covered["covered_population"].iloc[0] == pytest.approx(50)


def test_dissolved_catchments_avoid_double_counting():
    cells = gpd.GeoDataFrame(
        {"population": [100], "geometry": [Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])]},
        crs="EPSG:32633",
    )
    catchments = gpd.GeoDataFrame(
        {
            "station_id": [1, 2],
            "geometry": [
                Polygon([(0, 0), (5, 0), (5, 10), (0, 10)]),
                Polygon([(0, 0), (5, 0), (5, 10), (0, 10)]),
            ],
        },
        crs="EPSG:32633",
    )

    population, _ = population_within_catchments(cells, catchments, dissolve=True)
    assert population == pytest.approx(50)


def test_station_filter_share_requires_all_population_denominator():
    stations = gpd.GeoDataFrame({"station_id": [1], "geometry": [Point(1, 1)]}, crs="EPSG:32633")
    catchments = gpd.GeoDataFrame(
        {"station_id": [1], "geometry": [Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])]},
        crs="EPSG:32633",
    )
    urban_cells = gpd.GeoDataFrame(
        {"population": [10.0], "geometry": [Polygon([(0, 0), (1, 0), (1, 10), (0, 10)])]},
        crs="EPSG:32633",
    )

    kept, diagnostics = filter_stations_by_catchment_population(
        stations,
        catchments,
        urban_cells,
        min_population=500.0,
        min_urban_share=0.25,
    )

    assert kept.empty
    assert diagnostics["urban_share"].isna().all()


def test_station_filter_uses_catchment_population_share_with_denominator():
    stations = gpd.GeoDataFrame({"station_id": [1], "geometry": [Point(1, 1)]}, crs="EPSG:32633")
    catchments = gpd.GeoDataFrame(
        {"station_id": [1], "geometry": [Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])]},
        crs="EPSG:32633",
    )
    urban_cells = gpd.GeoDataFrame(
        {"population": [40.0], "geometry": [Polygon([(0, 0), (4, 0), (4, 10), (0, 10)])]},
        crs="EPSG:32633",
    )
    all_cells = gpd.GeoDataFrame(
        {"population": [100.0], "geometry": [Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])]},
        crs="EPSG:32633",
    )

    kept, diagnostics = filter_stations_by_catchment_population(
        stations,
        catchments,
        urban_cells,
        all_population_cells=all_cells,
        min_population=500.0,
        min_urban_share=0.25,
    )

    assert len(kept) == 1
    assert diagnostics["urban_share"].iloc[0] == pytest.approx(0.4)


def _grid(densities, *, water=None, size=1000.0):
    """Square 1 sq km cells; ``densities`` is a list of rows of people per cell."""
    records = []
    for row, values in enumerate(densities):
        for col, population in enumerate(values):
            x, y = col * size, row * size
            records.append(
                {
                    "cell_id": len(records),
                    "population": float(population),
                    "population_density": float(population),
                    "built_up_coverage": 0.0,
                    "is_water": bool(water and (row, col) in water),
                    "geometry": Polygon([(x, y), (x + size, y), (x + size, y + size), (x, y + size)]),
                }
            )
    return gpd.GeoDataFrame(records, crs="EPSG:32633")


def _classify(cells, **kwargs):
    adjacency = build_adjacency_graph(cells, mode="edge")
    params = {"centre_min_population": 10_000.0, "cluster_min_population": 1_000.0, **kwargs}
    return classify_degurba(cells, adjacency, **params).set_index("cell_id")["degurba_class"]


def test_degurba_centre_needs_density_and_cluster_population():
    cells = _grid([[2000, 2000, 2000, 0, 2000], [2000, 2000, 2000, 0, 400], [2000, 2000, 2000, 0, 0]])
    classes = _classify(cells, gap_fill_min_neighbors=5)
    assert (classes.loc[[0, 1, 2, 5, 6, 7, 10, 11, 12]] == "urban_centre").all()
    # The isolated dense cell (2000 people) is below the centre population threshold.
    assert classes.loc[4] == "urban_cluster"
    assert classes.loc[3] == "rural"


def test_degurba_fills_enclosed_holes_but_not_water():
    ring = [[2000] * 5, [2000, 2000, 2000, 2000, 2000], [2000, 2000, 50, 2000, 2000], [2000] * 5, [2000] * 5]
    classes = _classify(_grid(ring), gap_fill_min_neighbors=5)
    assert classes.loc[12] == "urban_centre"
    lake = _classify(_grid(ring, water={(2, 2)}), gap_fill_min_neighbors=5)
    assert lake.loc[12] == "rural"


def test_degurba_gap_fill_uses_neighbour_majority():
    cells = _grid([[2000, 2000, 2000], [2000, 100, 2000], [0, 0, 0]])
    assert _classify(cells, gap_fill_min_neighbors=3).loc[4] == "urban_centre"
    assert _classify(cells, gap_fill_min_neighbors=4).loc[4] != "urban_centre"


def test_degurba_study_area_is_the_centre_containing_the_city_point():
    cells = _grid([[2000] * 6 + [0] + [3000] * 6])
    classified = classify_degurba(cells, build_adjacency_graph(cells, mode="edge"), centre_min_population=10_000.0)
    chosen = select_study_centres(classified, centre_point=Point(500, 500))
    assert chosen == set(range(6))
    assert select_study_centres(classified) == set(range(7, 13))


def _square(size=100.0):
    return Polygon([(0, 0), (size, 0), (size, size), (0, size)])


def _coast(*lines):
    return gpd.GeoDataFrame({"natural": ["coastline"] * len(lines)}, geometry=list(lines), crs="EPSG:32633")


def test_sea_is_on_the_right_of_coastline_direction():
    # Walking east with land on the left (north) puts the sea in the south half.
    sea = sea_polygons_from_coastline(_coast(LineString([(-10, 40), (110, 40)])), _square())
    assert len(sea) == 1
    assert sea.geometry.iloc[0].area == pytest.approx(4000.0)
    assert sea.geometry.iloc[0].bounds[3] == pytest.approx(40.0)


def test_island_ring_keeps_land_inside():
    # Anticlockwise island ring (land on the left) inside a sea that fills the envelope.
    island = LineString([(40, 40), (60, 40), (60, 60), (40, 60), (40, 40)])
    shore = LineString([(-10, 90), (110, 90)])  # walking east: land north, sea south
    sea = sea_polygons_from_coastline(_coast(island, shore), _square())
    assert sea.geometry.union_all().area == pytest.approx(9000.0 - 400.0)


def test_truncated_coastline_falls_back_to_land():
    sea = sea_polygons_from_coastline(_coast(LineString([(-10, 40), (50, 40)])), _square())
    assert sea.empty
