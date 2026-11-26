import networkx as nx
import pandas as pd
import pytest
from datetime import date
import zipfile

gpd = pytest.importorskip("geopandas")
shapely = pytest.importorskip("shapely")

from shapely.geometry import LineString, Polygon

from transport.coverage import calculate_coverage
from transport.isochrones import build_station_isochrones, dissolve_buffered_lines, polygon_from_distances, reachable_edge_geometries
from transport.gtfs import consolidate_stations, match_gtfs_to_osm_stations, parse_feed_departures, prepare_r5_feed, r5_feed_path, route_mode
from transport.network import CsrGraph, nearest_nodes, super_source_distances
from transport.travel_time import _weighted_quantile, cumulative_accessibility
from publish_outputs import _check_consistency
from urban_area.catchment import filter_stations_by_catchment_population
from urban_area.metrics import add_urban_metrics
from urban_area.water import apply_water_exclusion, clip_cells_to_land


def _graph():
    graph = nx.MultiDiGraph()
    graph.graph["crs"] = "EPSG:32633"
    graph.add_node(1, x=0.0, y=0.0)
    graph.add_node(2, x=100.0, y=0.0)
    graph.add_node(3, x=200.0, y=0.0)
    graph.add_edge(1, 2, length=100.0, geometry=LineString([(0, 0), (100, 0)]))
    graph.add_edge(2, 3, length=100.0, geometry=LineString([(100, 0), (200, 0)]))
    return graph


def test_super_source_dijkstra_does_not_leave_pseudo_node():
    graph = _graph()
    before = set(graph.nodes)
    distances = super_source_distances(graph, [1, 3])
    assert distances[2] == pytest.approx(100.0)
    assert set(graph.nodes) == before


def test_synthetic_graph_isochrone_buffers_reachable_edges():
    polygon = polygon_from_distances(_graph(), {1: 0.0, 2: 100.0}, cutoff_m=125.0, buffer_m=10.0)
    assert polygon is not None
    assert polygon.area > 2_000
    assert polygon.bounds[2] == pytest.approx(135.0)


def test_reachable_edges_include_incident_directed_edges_but_skip_unrelated_component():
    graph = _graph()
    graph.add_node(4, x=-100.0, y=0.0)
    graph.add_edge(4, 1, length=100.0, geometry=LineString([(-100, 0), (0, 0)]))
    graph.add_node(10, x=10_000.0, y=0.0)
    graph.add_node(11, x=10_100.0, y=0.0)
    unrelated = LineString([(10_000, 0), (10_100, 0)])
    graph.add_edge(10, 11, length=100.0, geometry=unrelated)

    geometries = reachable_edge_geometries(graph, {1: 0.0}, cutoff_m=25.0)

    assert unrelated.wkb not in {geometry.wkb for geometry in geometries}
    assert min(geometry.bounds[0] for geometry in geometries) == pytest.approx(-25.0)


def test_isochrone_builder_resumes_existing_station_catchments(monkeypatch):
    graph = _graph()
    stations = gpd.GeoDataFrame(
        {"station_id": [1, 2], "geometry": gpd.points_from_xy([0, 100], [0, 0])},
        crs=graph.graph["crs"],
    )
    existing = gpd.GeoDataFrame(
        {"station_id": [1], "geometry": [Polygon([(-5, -5), (5, -5), (5, 5), (-5, 5)])]},
        crs=graph.graph["crs"],
    )
    monkeypatch.setattr("transport.isochrones.nearest_nodes", lambda *_: [1, 2])
    checkpoints = []

    result = build_station_isochrones(
        graph,
        stations,
        cutoff_m=125.0,
        buffer_m=10.0,
        existing=existing,
        checkpoint_every=1,
        progress_callback=lambda frame: checkpoints.append(frame.copy()),
    )

    assert sorted(result["station_id"]) == [1, 2]
    assert len(checkpoints) == 1
    assert sorted(checkpoints[0]["station_id"]) == [1, 2]


def test_weighted_coverage_returns_per_cell_fractions():
    cells = gpd.GeoDataFrame(
        {
            "cell_id": [1, 2],
            "population": [100.0, 100.0],
            "geometry": [
                Polygon([(0, 0), (10, 0), (10, 10), (0, 10)]),
                Polygon([(10, 0), (20, 0), (20, 10), (10, 10)]),
            ],
        },
        crs="EPSG:32633",
    )
    catchments = gpd.GeoDataFrame(
        {"station_id": [1], "geometry": [Polygon([(0, 0), (15, 0), (15, 10), (0, 10)])]},
        crs=cells.crs,
    )
    summary, covered = calculate_coverage(cells, catchments)
    assert summary["covered_population"] == pytest.approx(150.0)
    assert covered["covered_frac"].tolist() == pytest.approx([1.0, 0.5])


def test_station_filter_vectorizes_per_catchment_population():
    cells = gpd.GeoDataFrame(
        {
            "population": [100.0, 200.0],
            "geometry": [
                Polygon([(0, 0), (10, 0), (10, 10), (0, 10)]),
                Polygon([(10, 0), (20, 0), (20, 10), (10, 10)]),
            ],
        },
        crs="EPSG:32633",
    )
    stations = gpd.GeoDataFrame(
        {"station_id": [1, 2], "geometry": [gpd.points_from_xy([5], [5])[0], gpd.points_from_xy([15], [5])[0]]},
        crs=cells.crs,
    )
    catchments = gpd.GeoDataFrame(
        {
            "station_id": [1, 2],
            "geometry": [
                Polygon([(0, 0), (5, 0), (5, 10), (0, 10)]),
                Polygon([(10, 0), (20, 0), (20, 10), (10, 10)]),
            ],
        },
        crs=cells.crs,
    )

    kept, diagnostics = filter_stations_by_catchment_population(
        stations,
        catchments,
        cells,
        all_population_cells=cells,
        min_population=150.0,
        min_urban_share=2.0,
    )

    assert kept["station_id"].tolist() == [2]
    populations = diagnostics.set_index("station_id")["urban_catchment_population"]
    assert populations.loc[1] == pytest.approx(50.0)
    assert populations.loc[2] == pytest.approx(200.0)


def _square_cell(population=100.0, building_area=20.0):
    return gpd.GeoDataFrame(
        {
            "cell_id": [1],
            "population": [population],
            "total_building_area_sqm": [building_area],
            "building_area_sqm": [building_area],
            "geometry": [Polygon([(0, 0), (10, 0), (10, 10), (0, 10)])],
        },
        crs="EPSG:32633",
    )


def test_partially_wet_cell_keeps_population_and_uses_land_density():
    water = gpd.GeoDataFrame({"geometry": [Polygon([(0, 0), (6, 0), (6, 10), (0, 10)])]}, crs="EPSG:32633")
    annotated = apply_water_exclusion(_square_cell(), water, min_land_fraction=0.10)
    assert not bool(annotated["is_water"].iloc[0])
    assert annotated["land_area_sqm"].iloc[0] == pytest.approx(40.0)
    scored = add_urban_metrics(annotated, building_mode="footprint")
    # 100 people on 40 sq m of land, not 100 sq m of cell.
    assert scored["population_density"].iloc[0] == pytest.approx(100 / 40e-6)
    # 20 sq m of buildings on 40 sq m of land.
    assert scored["built_up_coverage"].iloc[0] == pytest.approx(0.5)


def test_open_water_cell_without_buildings_is_excluded():
    water = gpd.GeoDataFrame({"geometry": [Polygon([(0, 0), (10, 0), (10, 9.5), (0, 9.5)])]}, crs="EPSG:32633")
    annotated = apply_water_exclusion(_square_cell(population=1.0, building_area=0.0), water, min_land_fraction=0.10)
    assert bool(annotated["is_water"].iloc[0])
    scored = add_urban_metrics(annotated, building_mode="footprint")
    assert scored["urban_score_base"].iloc[0] == 0


def test_land_clip_preserves_population():
    water = gpd.GeoDataFrame({"geometry": [Polygon([(0, 0), (6, 0), (6, 10), (0, 10)])]}, crs="EPSG:32633")
    clipped = clip_cells_to_land(_square_cell(), water)
    assert clipped.geometry.area.iloc[0] == pytest.approx(40.0)
    assert clipped["population"].iloc[0] == pytest.approx(100.0)
    assert clipped["building_area_sqm"].iloc[0] == pytest.approx(20.0)
    assert clipped["water_clipped_fraction"].iloc[0] == pytest.approx(0.6)


def _write_feed(path, files):
    with zipfile.ZipFile(path, "w") as archive:
        for name, body in files.items():
            archive.writestr(name, body)


def _feed_files(route_type=2):
    return {
        "routes.txt": f"route_id,route_type\nr1,{route_type}\nb1,3\n",
        "trips.txt": (
            "route_id,service_id,trip_id,direction_id\n"
            "r1,weekday,t1,0\nr1,weekday,t2,0\nr1,weekday,t3,1\nb1,weekday,bus1,0\n"
        ),
        "calendar.txt": (
            "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
            "weekday,1,1,1,1,1,0,0,20260801,20260831\n"
        ),
        "stops.txt": (
            "stop_id,stop_name,stop_lat,stop_lon,location_type,parent_station\n"
            "parent,Central,60.0,10.0,1,\n"
            "p1,Central platform 1,60.0,10.0,0,parent\n"
            "p2,Central platform 2,60.0,10.001,0,parent\n"
            "end,Terminus,60.1,10.1,0,\n"
        ),
        "stop_times.txt": (
            "trip_id,departure_time,stop_id,stop_sequence\n"
            "t1,07:15:00,p1,1\nt1,07:30:00,end,2\n"
            "t2,08:15:00,p2,1\nt2,08:30:00,end,2\n"
            "t3,07:45:00,end,1\nt3,08:00:00,p1,2\n"
            "bus1,07:20:00,p1,1\nbus1,07:40:00,end,2\n"
        ),
    }


def test_route_mode_covers_extended_route_types():
    assert route_mode(901) == "tram"
    assert route_mode(902) == "tram"
    assert route_mode(401) == "metro"
    assert route_mode(106) == "rail"
    assert route_mode(11) == "bus"
    assert route_mode(715) is None
    assert route_mode(202) is None


def test_gtfs_departures_use_busiest_direction_and_skip_final_stops(tmp_path):
    path = tmp_path / "feed.zip"
    _write_feed(path, _feed_files())
    stations = parse_feed_departures(path, service_date=date(2026, 8, 11), feed_prefix="test", modes=("rail",))
    by_id = stations.set_index("stop_id")
    # Central: t1 and t2 depart in direction 0; t3 ends there, so it is not a departure.
    assert by_id.loc["test:parent", "departures_per_hr"] == pytest.approx(1.0)
    assert by_id.loc["test:parent", "departures_per_hr_total"] == pytest.approx(1.0)
    # Terminus: only t3 departs from it.
    assert by_id.loc["test:end", "departures_per_hr"] == pytest.approx(0.5)
    assert set(stations["mode"]) == {"rail"}


def test_gtfs_tram_route_types_are_counted(tmp_path):
    path = tmp_path / "feed.zip"
    _write_feed(path, _feed_files(route_type=901))
    stations = parse_feed_departures(path, service_date=date(2026, 8, 11), modes=("tram", "bus"))
    central = stations[stations["stop_id"] == "feed:parent"].set_index("mode")
    assert central.loc["tram", "departures_per_hr"] == pytest.approx(1.0)
    assert central.loc["bus", "departures_per_hr"] == pytest.approx(0.5)


def test_gtfs_rejects_dates_without_service(tmp_path):
    path = tmp_path / "feed.zip"
    _write_feed(path, _feed_files())
    with pytest.raises(ValueError, match="outside the feed calendar"):
        parse_feed_departures(path, service_date=date(2026, 10, 20))
    with pytest.raises(ValueError, match="No services are active"):
        parse_feed_departures(path, service_date=date(2026, 8, 15))


def _gtfs_stations(rows):
    return gpd.GeoDataFrame(
        {
            "stop_id": [row[0] for row in rows],
            "stop_name": [row[1] for row in rows],
            "mode": [row[2] for row in rows],
            "departures_per_hr": [row[3] for row in rows],
            "departures_per_hr_total": [row[3] * 2 for row in rows],
            "departures_day": [100 for _ in rows],
        },
        geometry=gpd.points_from_xy([row[4] for row in rows], [0.0 for _ in rows]),
        crs="EPSG:32633",
    )


def test_consolidate_stations_merges_same_name_and_mode_only():
    stations = _gtfs_stations(
        [
            ("a", "Jernbanetorget", "tram", 10.0, 0.0),
            ("b", "Jernbanetorget", "tram", 14.0, 60.0),
            ("c", "Jernbanetorget", "metro", 30.0, 30.0),
            ("d", "Jernbanetorget", "tram", 8.0, 5_000.0),
        ]
    )
    merged = consolidate_stations(stations, metric_crs="EPSG:32633", merge_distance_m=150.0)
    assert len(merged) == 3
    near_tram = merged[(merged["mode"] == "tram") & (merged["gtfs_station_ids"] == "a;b")].iloc[0]
    assert near_tram["departures_per_hr"] == pytest.approx(14.0)
    assert near_tram["departures_per_hr_total"] == pytest.approx(48.0)


def test_osm_matching_is_mode_aware_and_rerunnable():
    gtfs = _gtfs_stations(
        [("bus", "Lysaker", "bus", 20.0, 10.0), ("rail", "Lysaker stasjon", "rail", 8.0, 150.0)]
    )
    osm = gpd.GeoDataFrame(
        {"station_id": [0], "name": ["Lysaker"], "mode": ["rail"]},
        geometry=gpd.points_from_xy([0.0], [0.0]),
        crs="EPSG:32633",
    )
    matched, _ = match_gtfs_to_osm_stations(osm, gtfs, metric_crs="EPSG:32633", min_name_similarity=0.4)
    assert matched["gtfs_stop_id"].iloc[0] == "rail"
    assert matched["departures_per_hr"].iloc[0] == pytest.approx(8.0)
    rematched, _ = match_gtfs_to_osm_stations(matched, gtfs, metric_crs="EPSG:32633", min_name_similarity=0.4)
    assert rematched["departures_per_hr"].iloc[0] == pytest.approx(8.0)


def test_scipy_distances_match_networkx():
    graph = _graph()
    graph.add_edge(2, 1, length=100.0)
    graph.add_edge(3, 2, length=100.0)
    csr = CsrGraph(graph)
    [scipy_distances] = csr.distances_from([1], cutoff_m=150.0)
    assert scipy_distances == pytest.approx(nx.single_source_dijkstra_path_length(graph, 1, cutoff=150.0, weight="length"))
    nearest = csr.multi_source_distances([1, 3], cutoff_m=1_000.0)
    assert nearest == pytest.approx({1: 0.0, 2: 100.0, 3: 0.0})


def test_nearest_nodes_skip_isolated_fragments():
    graph = _graph()
    graph.add_node(99, x=1.0, y=1.0)
    stations = gpd.GeoDataFrame({"station_id": [1]}, geometry=gpd.points_from_xy([1.0], [1.0]), crs="EPSG:32633")
    assert nearest_nodes(graph, stations) == [1]
    assert nearest_nodes(graph, stations, main_component_only=False) == [99]


def test_cumulative_accessibility_weights_by_population():
    origins = pd.DataFrame({"id": ["a", "b", "c"], "population": [100.0, 100.0, 200.0]})
    matrix = pd.DataFrame(
        {
            "from_id": ["a", "a", "a", "b", "b", "b", "c", "c", "c"],
            "to_id": ["a", "b", "c"] * 3,
            "travel_time": [0, 20, 40, 20, 0, None, 40, None, 0],
        }
    )
    metrics = cumulative_accessibility(matrix, origins, cutoffs_min=(30, 45))
    # Reach within 30 min: a->a,b (200), b->a,b (200), c->c (200), each of 400 people.
    assert metrics["access_pop_30min_pct"] == pytest.approx(50.0)
    # Within 45 min, a also reaches c, and c reaches a.
    assert metrics["access_pop_45min_pct"] == pytest.approx(100 * (100 * 1.0 + 100 * 0.5 + 200 * 0.75) / 400)


def test_weighted_median_treats_unreachable_as_slowest():
    values = pd.Series([10.0, None, None])
    weights = pd.Series([1.0, 1.0, 1.0])
    assert _weighted_quantile(values, weights, 0.5) is None
    assert _weighted_quantile(pd.Series([10.0, 20.0, None]), weights, 0.5) == pytest.approx(20.0)


def test_publisher_rejects_metrics_from_a_different_urban_area():
    urban = {"urban_population": 792_054.0}
    stale = {"urban_population": 804_250.0}
    with pytest.raises(RuntimeError, match="re-run the transport stage"):
        _check_consistency("Oslo", urban, stale, {}, allow_incomplete=False)
    current = _check_consistency("Oslo", urban, stale, {}, allow_incomplete=True)
    assert current == {"transport": False, "gtfs": False}
    assert _check_consistency("Oslo", urban, dict(urban), dict(urban), allow_incomplete=False) == {
        "transport": True,
        "gtfs": True,
    }


def test_tiled_dissolve_matches_direct_buffer():
    lines = [LineString([(x * 300.0, y * 300.0), (x * 300.0 + 350.0, y * 300.0)]) for x in range(20) for y in range(20)]
    direct = shapely.union_all(lines).buffer(25.0)
    tiled = dissolve_buffered_lines(lines, 25.0, tile_m=1_000.0, small=0)
    assert tiled.area == pytest.approx(direct.area, rel=1e-3)
    assert tiled.symmetric_difference(direct).area < 1e-3 * direct.area


def test_r5_feed_is_clipped_and_drops_multi_day_trips(tmp_path):
    files = _feed_files()
    files["stop_times.txt"] = files["stop_times.txt"].replace("trip_id,departure_time,stop_id,stop_sequence", "trip_id,arrival_time,departure_time,stop_id,stop_sequence")
    rows = files["stop_times.txt"].split(chr(10))
    header, body = rows[0], [row for row in rows[1:] if row]
    body = [",".join([r.split(",")[0], r.split(",")[1], *r.split(",")[1:]]) for r in body]
    body.append("long,80:00:00,80:00:00,p1,1")
    body.append("long,81:00:00,81:00:00,p2,2")
    files["stop_times.txt"] = chr(10).join([header, *body, ""])
    files["trips.txt"] += "r1,weekday,long,0" + chr(10)
    source = tmp_path / "feed.zip"
    _write_feed(source, files)
    output = prepare_r5_feed(source, tmp_path / "feed_r5.zip", bbox=(9.9, 59.9, 10.05, 60.05))
    with zipfile.ZipFile(output) as archive:
        stop_times = pd.read_csv(archive.open("stop_times.txt"), dtype=str)
        names = set(archive.namelist())
    assert "long" not in set(stop_times["trip_id"])
    # "end" (Terminus) lies outside the box, so trips keep only their Central stops.
    assert set(stop_times["stop_id"]) <= {"p1", "p2"}
    assert "transfers.txt" not in names


def test_whitespace_parent_station_keeps_each_stop_as_its_own_station(tmp_path):
    files = _feed_files()
    # HSL pads empty parent_station values with a single space.
    files["stops.txt"] = (
        "stop_id,stop_name,stop_lat,stop_lon,location_type,parent_station\n"
        "p1,Central platform 1,60.0,10.0,0, \n"
        "p2,Central platform 2,60.0,10.001,0, \n"
        "end,Terminus,60.1,10.1,0, \n"
    )
    rows = [row.split(",") for row in files["stop_times.txt"].strip().split("\n")]
    files["stop_times.txt"] = "".join(",".join([r[0], "arrival_time" if i == 0 else r[1], *r[1:]]) + "\n" for i, r in enumerate(rows))
    path = tmp_path / "feed.zip"
    _write_feed(path, files)
    stations = parse_feed_departures(path, service_date=date(2026, 8, 11), feed_prefix="test", modes=("rail", "bus"))
    rail = stations[stations["mode"] == "rail"].set_index("stop_id")
    assert set(rail.index) == {"test:p1", "test:p2", "test:end"}
    assert rail.loc["test:p1", "departures_per_hr"] == pytest.approx(0.5)
    assert rail.loc["test:p2", "departures_per_hr"] == pytest.approx(0.5)
    assert set(stations.loc[stations["mode"] == "bus", "stop_id"]) == {"test:p1"}

    output = prepare_r5_feed(path, tmp_path / "feed_r5.zip", bbox=(9.9, 59.9, 10.2, 60.2))
    with zipfile.ZipFile(output) as archive:
        stops = pd.read_csv(archive.open("stops.txt"), dtype=str, keep_default_na=False)
    assert set(stops["stop_id"]) == {"p1", "p2", "end"}
    assert set(stops["parent_station"]) == {""}


def test_r5_feed_names_are_unique_per_city_and_feed_content(tmp_path):
    # r5py caches inputs by base name, so same-named feeds from two cities must not collide.
    oslo, helsinki = tmp_path / "oslo" / "feed_1.zip", tmp_path / "helsinki" / "feed_1.zip"
    for path, content in [(oslo, b"oslo timetable"), (helsinki, b"helsinki timetable")]:
        path.parent.mkdir()
        path.write_bytes(content)
    oslo_r5, helsinki_r5 = r5_feed_path(oslo, "oslo"), r5_feed_path(helsinki, "helsinki")
    assert oslo_r5.parent == oslo.parent and oslo_r5.name.startswith("oslo_feed_1_")
    assert oslo_r5.name != helsinki_r5.name
    assert r5_feed_path(oslo, "oslo") == oslo_r5  # stable for unchanged content
    oslo.write_bytes(b"oslo timetable, new version")
    assert r5_feed_path(oslo, "oslo").name != oslo_r5.name  # a new feed gets a new name


def test_feed_headers_expand_from_environment_and_stay_out_of_provenance(tmp_path, monkeypatch):
    import io as _io
    import json
    from dataclasses import asdict

    import transport.gtfs as gtfs_module
    from transport.config import TransportConfig, load_transport_config
    from transport.pipeline import _jsonable

    secret = "s3cr3t-subscription-value"
    monkeypatch.setenv("TEST_FEED_KEY", secret)
    url = "https://example.invalid/gtfs.zip"
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"cities": {"Test, Land": {
        "city_slug": "test",
        "gtfs": {"feed_urls": [url, "https://example.invalid/open.zip?key=literalkey"],
                 "feed_headers": {url: {"Ocp-Apim-Subscription-Key": "${TEST_FEED_KEY}"}}},
    }}}), encoding="utf-8")
    config = load_transport_config(config_path, "Test, Land")
    config = TransportConfig(**{**asdict(config), "cache_dir": tmp_path / "cache"})
    assert config.gtfs_feed_headers[url] == {"Ocp-Apim-Subscription-Key": "${TEST_FEED_KEY}"}

    seen = {}

    class _Response(_io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(request, timeout, context):
        seen[request.full_url] = dict(request.header_items())
        return _Response(b"zipbytes")

    monkeypatch.setattr(gtfs_module, "urlopen", fake_urlopen)
    paths = gtfs_module.download_gtfs_feeds(config)
    assert seen[url]["Ocp-apim-subscription-key"] == secret
    assert "Ocp-apim-subscription-key" not in seen["https://example.invalid/open.zip?key=literalkey"]

    records = gtfs_module.feed_provenance(config, paths)
    assert records[0]["sha256"] == __import__("hashlib").sha256(b"zipbytes").hexdigest()
    assert records[1]["source"].endswith("key=***")
    provenance_text = json.dumps({"files": records, "config": _jsonable(asdict(config))})
    assert secret not in provenance_text
    assert "literalkey" not in json.dumps(records)

    monkeypatch.delenv("TEST_FEED_KEY")
    with pytest.raises(RuntimeError, match="unresolved environment variable") as error:
        gtfs_module.download_gtfs_feeds(config, force=True)
    assert secret not in str(error.value)


def test_missing_direction_id_is_inferred_from_bearing_to_next_stop(tmp_path):
    # A north-south line through Mid with two northern branches (different
    # headsigns) and one southern destination, and no direction_id at all.
    files = {
        "routes.txt": "route_id,route_type\nr1,2\n",
        "trips.txt": (
            "route_id,service_id,trip_id,trip_headsign\n"
            "r1,weekday,n1,North A\nr1,weekday,n2,North A\nr1,weekday,n3,North B\nr1,weekday,n4,North B\n"
            "r1,weekday,s1,South\nr1,weekday,s2,South\n"
        ),
        "calendar.txt": (
            "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
            "weekday,1,1,1,1,1,0,0,20260801,20260831\n"
        ),
        "stops.txt": (
            "stop_id,stop_name,stop_lat,stop_lon\n"
            "south,South,59.90,10.000\nmid,Mid,59.95,10.000\nnorth_a,North A,60.00,9.990\nnorth_b,North B,60.00,10.010\n"
        ),
        "stop_times.txt": (
            "trip_id,departure_time,stop_id,stop_sequence\n"
            "n1,07:00:00,south,1\nn1,07:10:00,mid,2\nn1,07:20:00,north_a,3\n"
            "n2,08:00:00,south,1\nn2,08:10:00,mid,2\nn2,08:20:00,north_a,3\n"
            "n3,07:30:00,south,1\nn3,07:40:00,mid,2\nn3,07:50:00,north_b,3\n"
            "n4,08:30:00,south,1\nn4,08:40:00,mid,2\nn4,08:50:00,north_b,3\n"
            "s1,07:00:00,north_a,1\ns1,07:15:00,mid,2\ns1,07:30:00,south,3\n"
            "s2,08:00:00,north_b,1\ns2,08:15:00,mid,2\ns2,08:30:00,south,3\n"
        ),
    }
    path = tmp_path / "feed.zip"
    _write_feed(path, files)
    stations = parse_feed_departures(path, service_date=date(2026, 8, 11), feed_prefix="t", modes=("rail",))
    mid = stations.set_index("stop_id").loc["t:mid"]
    # Four northbound departures in two hours pool into one direction despite two headsigns.
    assert mid["departures_per_hr"] == pytest.approx(2.0)
    assert mid["departures_per_hr_total"] == pytest.approx(3.0)


def test_direction_id_still_takes_precedence_over_bearing(tmp_path):
    path = tmp_path / "feed.zip"
    _write_feed(path, _feed_files())
    stations = parse_feed_departures(path, service_date=date(2026, 8, 11), feed_prefix="test", modes=("rail",))
    assert stations.set_index("stop_id").loc["test:parent", "departures_per_hr"] == pytest.approx(1.0)
