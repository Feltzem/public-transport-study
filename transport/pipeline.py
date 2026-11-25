"""End-to-end GTFS-station, walking-catchment, and coverage pipeline.

Stations come from the GTFS feed for a pinned service date (in service by
construction, with mode and frequency); OSM supplies rail linework for maps
and, optionally, the station features themselves (``station_source='osm'``).
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any

import geopandas as gpd
from shapely.ops import unary_union

from .config import TransportConfig
from .coverage import calculate_coverage, frequency_eligible_catchments, polygon_coverage
from .gtfs import (
    RAIL_TIER_MODES,
    consolidate_stations,
    download_gtfs_feeds,
    feed_provenance,
    load_departures,
    match_gtfs_to_osm_stations,
)
from .io import list_layers, load_required_layer, load_transport_inputs, write_json, write_layer
from .isochrones import build_combined_isochrone, build_station_isochrones
from .network import CsrGraph, load_or_download_walk_graph
from .rail import filter_rail_stations, prefilter_stations_by_possible_urban_reach, query_osm_rail


def _jsonable(value):
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _catchment_cache_layer(config: TransportConfig, stations, graph_path: Path) -> str:
    digest = sha256()
    digest.update(f"{config.walking_distance_m}|{config.isochrone_buffer_m}".encode())
    if graph_path.exists():
        stat = graph_path.stat()
        digest.update(f"{graph_path.name}|{stat.st_size}|{stat.st_mtime_ns}".encode())
    for station_id, geometry in stations[["station_id", "geometry"]].itertuples(index=False, name=None):
        digest.update(str(station_id).encode())
        digest.update(geometry.wkb)
    return f"isochrone_candidates_{config.resolved_slug()}_{digest.hexdigest()[:12]}"


def _osm_rail(config: TransportConfig, boundary, crs: str, *, force_osm: bool):
    slug = config.resolved_slug()
    lines_layer = f"rail_lines_osm_candidates_{slug}"
    stations_layer = f"rail_stations_osm_candidates_{slug}"
    if not force_osm and {lines_layer, stations_layer}.issubset(list_layers(config.rail_gpkg)):
        return (
            load_required_layer(config.rail_gpkg, lines_layer, crs=crs),
            load_required_layer(config.rail_gpkg, stations_layer, crs=crs),
        )
    rail_lines, stations = query_osm_rail(boundary, crs, rail_modes=config.rail_modes)
    write_layer(rail_lines, config.rail_gpkg, lines_layer)
    write_layer(stations, config.rail_gpkg, stations_layer)
    return rail_lines, stations


def _rail_stations(config: TransportConfig, departures_metric, osm_stations, crs: str):
    if config.station_source == "osm":
        matched, _ = match_gtfs_to_osm_stations(
            osm_stations,
            departures_metric,
            metric_crs=crs,
            tolerance_m=config.gtfs_match_tolerance_m,
            min_name_similarity=config.gtfs_min_name_similarity,
        )
        return matched
    if config.station_source != "gtfs":
        raise ValueError(f"station_source must be 'gtfs' or 'osm', not {config.station_source!r}")
    rail = departures_metric[departures_metric["mode"].isin(RAIL_TIER_MODES)]
    return consolidate_stations(rail, metric_crs=crs, merge_distance_m=config.station_merge_distance_m)


def _all_mode_tiers(config, graph, csr, departures_metric, urban_cells, frequent_rail_catchments, buildings, crs):
    """Dissolved all-mode catchments: any stop, frequent stop, frequent any mode."""
    stops = consolidate_stations(
        departures_metric[departures_metric["mode"].isin(config.all_modes)],
        metric_crs=crs,
        merge_distance_m=config.station_merge_distance_m,
    )
    stops = prefilter_stations_by_possible_urban_reach(
        stops, urban_cells, walking_distance_m=config.bus_walking_distance_m
    )
    frequent_stops = stops[stops["departures_per_hr"] >= config.frequent_dep_per_hr]
    kwargs = {"cutoff_m": config.bus_walking_distance_m, "buffer_m": config.isochrone_buffer_m, "csr": csr}
    any_stop = build_combined_isochrone(graph, stops, **kwargs)
    frequent_stop = build_combined_isochrone(graph, frequent_stops, **kwargs)
    frequent_rail = unary_union(list(frequent_rail_catchments.geometry)) if not frequent_rail_catchments.empty else None
    parts = [geometry for geometry in [frequent_stop, frequent_rail] if geometry is not None and not geometry.is_empty]
    frequent_any = unary_union(parts) if parts else None

    any_summary, _ = polygon_coverage(urban_cells, any_stop, buildings=buildings)
    frequent_any_summary, frequent_any_cells = polygon_coverage(urban_cells, frequent_any, buildings=buildings)
    tiers = gpd.GeoDataFrame(
        {
            "tier": ["any_stop", "frequent_stop", "frequent_any_mode"],
            "walk_distance_m": [config.bus_walking_distance_m, config.bus_walking_distance_m, None],
            "geometry": [any_stop, frequent_stop, frequent_any],
        },
        geometry="geometry",
        crs=crs,
    )
    tiers = tiers[tiers.geometry.notna()].copy()
    summary = {
        "stop_walking_distance_m": config.bus_walking_distance_m,
        "n_stops_all_modes": int(len(stops)),
        "n_stops_frequent": int(len(frequent_stops)),
        "stops_by_mode": {str(k): int(v) for k, v in stops["mode"].value_counts().items()},
        "pct_any_stop": any_summary["pct_within_15min"],
        "covered_population_any_stop": any_summary["covered_population"],
        "pct_frequent_any_mode": frequent_any_summary["pct_within_15min"],
        "covered_population_frequent_any_mode": frequent_any_summary["covered_population"],
    }
    return summary, stops, tiers, frequent_any_cells


def run_transport_pipeline(
    config: TransportConfig,
    *,
    force_network: bool = False,
    force_osm: bool = False,
    force_gtfs: bool = False,
    write_outputs: bool = True,
) -> dict[str, Any]:
    inputs = load_transport_inputs(config)
    boundary = inputs["boundary"]
    urban_cells = inputs["urban_cells"]
    analysis_cells = inputs["analysis_cells"]
    buildings = inputs["buildings"]
    crs = config.resolved_metric_crs()
    slug = config.resolved_slug()

    rail_lines, osm_stations = _osm_rail(config, boundary, crs, force_osm=force_osm)
    service_date = config.resolved_service_date()
    start_hour, end_hour = config.gtfs_window
    feeds = download_gtfs_feeds(config, force=force_gtfs)
    min_lon, min_lat, max_lon, max_lat = boundary.to_crs("EPSG:4326").total_bounds
    departures = load_departures(
        feeds,
        service_date=service_date,
        start_hour=start_hour,
        end_hour=end_hour,
        modes=tuple(config.all_modes),
        bbox=(min_lon - 0.05, min_lat - 0.05, max_lon + 0.05, max_lat + 0.05),
    )
    departures_metric = departures.to_crs(crs)

    stations = _rail_stations(config, departures_metric, osm_stations, crs)
    stations = prefilter_stations_by_possible_urban_reach(
        stations,
        urban_cells,
        walking_distance_m=config.walking_distance_m,
    )
    stations["station_id"] = range(len(stations))
    if stations.empty:
        raise RuntimeError(f"No rail stations are close enough to serve {config.city_name}'s urban cells")
    graph = load_or_download_walk_graph(
        boundary,
        cache_path=config.walk_graph_path(),
        metric_crs=crs,
        network_type=config.network_type,
        force=force_network,
    )
    csr = CsrGraph(graph)
    catchment_cache_layer = _catchment_cache_layer(config, stations, config.walk_graph_path())
    existing_catchments = None
    if catchment_cache_layer in list_layers(config.isochrone_gpkg):
        existing_catchments = load_required_layer(config.isochrone_gpkg, catchment_cache_layer, crs=crs)

    def checkpoint_catchments(frame):
        write_layer(frame, config.isochrone_gpkg, catchment_cache_layer)

    catchments = build_station_isochrones(
        graph,
        stations,
        cutoff_m=config.walking_distance_m,
        buffer_m=config.isochrone_buffer_m,
        existing=existing_catchments,
        checkpoint_every=50,
        progress_callback=checkpoint_catchments,
        csr=csr,
    )
    kept_stations, station_diagnostics = filter_rail_stations(
        stations,
        catchments,
        urban_cells,
        all_population_cells=analysis_cells,
        buildings=buildings,
        min_population=config.min_station_population,
        min_urban_share=config.min_station_urban_share,
    )
    kept_ids = set(kept_stations["station_id"])
    kept_catchments = catchments[catchments["station_id"].isin(kept_ids)].copy()
    if kept_catchments.empty:
        raise RuntimeError(f"No {config.city_name} stations passed catchment-population filtering")
    summary, coverage_cells = calculate_coverage(urban_cells, kept_catchments, buildings=buildings)
    frequent_catchments = frequency_eligible_catchments(
        kept_catchments, kept_stations, minimum_departures_per_hour=config.frequent_dep_per_hr
    )
    frequency_summary, frequency_cells = calculate_coverage(urban_cells, frequent_catchments, buildings=buildings)
    all_modes_summary, all_mode_stops, all_mode_tiers, frequent_any_cells = _all_mode_tiers(
        config, graph, csr, departures_metric, urban_cells, frequent_catchments, buildings, crs
    )
    return _finish(
        config, slug, crs, service_date, feeds, write_outputs,
        rail_lines=rail_lines, stations=stations, kept_stations=kept_stations,
        station_diagnostics=station_diagnostics, kept_catchments=kept_catchments,
        coverage_cells=coverage_cells, frequency_cells=frequency_cells,
        frequent_any_cells=frequent_any_cells, all_mode_stops=all_mode_stops, all_mode_tiers=all_mode_tiers,
        summary=summary, frequency_summary=frequency_summary, all_modes_summary=all_modes_summary,
    )


def _finish(config, slug, crs, service_date, feeds, write_outputs, **frames) -> dict[str, Any]:
    kept_stations = frames["kept_stations"].merge(
        frames["station_diagnostics"].drop(columns=["geometry"], errors="ignore"),
        on="station_id",
        how="left",
    )
    kept_catchments = frames["kept_catchments"].merge(
        kept_stations[["station_id", "name", "mode", "departures_per_hr"]], on="station_id", how="left"
    )
    kept_catchments["walk_minutes"] = config.walk_minutes()
    kept_catchments["walk_distance_m"] = config.walking_distance_m
    run_date = datetime.now(timezone.utc).date().isoformat()
    layers = {
        "rail_lines": frames["rail_lines"],
        "rail_stations": kept_stations,
        "isochrones": kept_catchments,
        "coverage_cells": frames["coverage_cells"],
        "frequency_cells": frames["frequency_cells"],
        "frequent_any_cells": frames["frequent_any_cells"],
        "all_mode_stops": frames["all_mode_stops"],
        "all_mode_tiers": frames["all_mode_tiers"],
    }
    for frame in layers.values():
        frame["city"] = config.city_name
        frame["city_slug"] = slug
        frame["run_date"] = run_date

    summary = frames["summary"]
    frequency = frames["frequency_summary"]
    departures = kept_stations["departures_per_hr"]
    stations_summary = {
        "candidate_station_count": int(len(frames["stations"])),
        "kept_station_count": int(len(kept_stations)),
        "kept_by_mode": {str(k): int(v) for k, v in kept_stations["mode"].value_counts().items()},
        "frequent_station_count": int((departures >= config.frequent_dep_per_hr).sum()),
        "dep_per_hr_mean": float(departures.mean()),
        "dep_per_hr_median": float(departures.median()),
        "zero_departure_station_count": int((departures == 0).sum()),
    }
    frequency_block = {
        "service_date": service_date.isoformat(),
        "window": f"{config.gtfs_window[0]:02d}:00-{config.gtfs_window[1]:02d}:00",
        "frequency_definition": "departures per hour in the busiest direction; final stops excluded",
        "frequent_threshold_dep_per_hr": config.frequent_dep_per_hr,
        "covered_population": frequency["covered_population"],
        "pct_within_15min_frequent": frequency["pct_within_15min"],
    }
    validation = {
        "city": config.city_name,
        **stations_summary,
        "rail_line_count": int(len(frames["rail_lines"])),
        "catchment_count": int(len(kept_catchments)),
        "non_empty": {name: not frame.empty for name, frame in layers.items()},
        "coverage": summary,
        "frequency": frequency_block,
        "all_modes": frames["all_modes_summary"],
    }
    provenance = {
        "city_name": config.city_name,
        "city_slug": slug,
        "metric_crs": crs,
        "run_started_at_utc": datetime.now(timezone.utc).isoformat(),
        "station_source": config.station_source,
        "coverage_method": "building_weighted_with_area_weighted_fallback",
        "station_filter_method": "walking_catchment_population_or_urban_share",
        "network_cache_key": config.walk_graph_path().name,
        "gtfs_feeds": [str(path) for path in feeds],
        "gtfs_feed_files": feed_provenance(config, feeds),
        "config": _jsonable(asdict(config)),
        "legacy_oslo_reference_pct": 60.65 if slug == "oslo" else None,
        "legacy_centroid_pct_this_run": summary["legacy_centroid_pct"],
        "weighted_pct_this_run": summary["pct_within_15min"],
    }

    outputs: dict[str, str] = {}
    if write_outputs:
        targets = {
            "rail_lines": (config.rail_gpkg, f"rail_lines_{slug}"),
            "rail_stations": (config.rail_gpkg, f"rail_stations_{slug}"),
            "isochrones": (config.isochrone_gpkg, f"isochrones_15min_{slug}"),
            "coverage_cells": (config.isochrone_gpkg, f"coverage_cells_15min_{slug}"),
            "frequency_cells": (config.isochrone_gpkg, f"coverage_cells_frequency_{slug}"),
            "frequent_any_cells": (config.isochrone_gpkg, f"coverage_cells_frequent_any_mode_{slug}"),
            "all_mode_stops": (config.rail_gpkg, f"pt_stops_all_modes_{slug}"),
            "all_mode_tiers": (config.isochrone_gpkg, f"catchments_all_modes_{slug}"),
        }
        outputs = {name: write_layer(layers[name], path, layer) for name, (path, layer) in targets.items()}
        provenance_dir = config.data_dir / "provenance"
        write_json(provenance_dir / f"{slug}_transport_validation.json", validation)
        write_json(provenance_dir / f"{slug}_transport_provenance.json", {**provenance, "outputs": outputs})

    return {
        "config": config,
        "rail_lines": layers["rail_lines"],
        "stations": kept_stations,
        "station_diagnostics": frames["station_diagnostics"],
        "isochrones": kept_catchments,
        "coverage_cells": layers["coverage_cells"],
        "all_mode_stops": layers["all_mode_stops"],
        "summary": summary,
        "frequency": frequency_block,
        "all_modes": frames["all_modes_summary"],
        "validation": validation,
        "provenance": provenance,
        "outputs": outputs,
    }
