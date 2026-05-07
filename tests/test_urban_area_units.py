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
from urban_area.growth import run_growing_method
from urban_area.io import load_population_cells
from urban_area.refinement import allocate_population_dasymetric, refine_urban_edges
from urban_area.geometry import build_adjacency_graph
from urban_area.metrics import add_urban_metrics
from urban_area.roads import aggregate_roads_to_cells
from urban_area.schemas import validate_buildings


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
