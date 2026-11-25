"""End-to-end urban-area detection pipeline."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import geopandas as gpd
import requests
from shapely.geometry import Point

from .buildings import aggregate_buildings_to_cells
from .config import CityConfig
from .growth import component_retention_kwargs, detect_urban_cells
from .geometry import build_adjacency_graph
from .io import (
    hull_from_cells,
    load_buildings,
    load_city_boundary,
    load_population_cells,
    load_roads,
    save_pipeline_outputs,
)
from .metrics import NORMALISED_SCORE_COLUMNS, add_absolute_metrics, add_urban_metrics
from .osm_pbf import city_pbf_path, load_pbf_roads
from .refinement import refine_urban_edges
from .roads import aggregate_roads_to_cells, normalize_roads
from .schemas import validate_analysis_cells, validate_buildings, validate_population_cells, validate_roads
from .validation import build_validation_report
from .water import (
    OVERPASS_ENDPOINTS,
    OVERPASS_MAX_QUERY_AREA_SQM,
    apply_water_exclusion,
    clip_cells_to_land,
    load_or_query_water_mask,
)


def _query_osm_buildings(city_boundary_metric: gpd.GeoDataFrame, metric_crs: str) -> gpd.GeoDataFrame:
    import osmnx as ox

    query_polygon = city_boundary_metric.to_crs("EPSG:4326").geometry.iloc[0]
    buildings = ox.features_from_polygon(query_polygon, tags={"building": True})
    if buildings.empty:
        return gpd.GeoDataFrame(columns=["geometry"], geometry="geometry", crs=metric_crs)
    return buildings.to_crs(metric_crs)


def _query_osm_roads(city_boundary_metric: gpd.GeoDataFrame, metric_crs: str, road_tags: dict[str, list[str]]) -> gpd.GeoDataFrame:
    import osmnx as ox

    query_polygon = city_boundary_metric.to_crs("EPSG:4326").geometry.iloc[0]
    previous_url = ox.settings.overpass_url
    previous_timeout = ox.settings.requests_timeout
    previous_max_area = ox.settings.max_query_area_size
    endpoints = tuple(dict.fromkeys((*OVERPASS_ENDPOINTS, previous_url)))
    last_error = None
    try:
        ox.settings.requests_timeout = max(previous_timeout, 300)
        ox.settings.max_query_area_size = min(previous_max_area, OVERPASS_MAX_QUERY_AREA_SQM)
        for endpoint in endpoints:
            ox.settings.overpass_url = endpoint
            try:
                roads = ox.features_from_polygon(query_polygon, tags=road_tags)
                break
            except requests.exceptions.RequestException as exc:
                last_error = exc
        else:
            raise RuntimeError(f"All Overpass endpoints failed while querying roads: {last_error}") from last_error
    finally:
        ox.settings.overpass_url = previous_url
        ox.settings.requests_timeout = previous_timeout
        ox.settings.max_query_area_size = previous_max_area
    if roads.empty:
        return gpd.GeoDataFrame(columns=["geometry"], geometry="geometry", crs=metric_crs)
    return roads.to_crs(metric_crs)


def _config_to_jsonable(config: CityConfig) -> dict[str, Any]:
    def convert(value):
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, dict):
            return {key: convert(item) for key, item in value.items()}
        if isinstance(value, list):
            return [convert(item) for item in value]
        return value

    return convert(asdict(config))


def _centre_point(config: CityConfig, metric_crs: str):
    if config.centre_lon is None or config.centre_lat is None:
        return None
    point = gpd.GeoSeries([Point(config.centre_lon, config.centre_lat)], crs="EPSG:4326")
    return point.to_crs(metric_crs).iloc[0]


def _detection_provenance(config: CityConfig) -> dict[str, Any]:
    growth = config.growth
    if growth.method != "degurba":
        return {"method": growth.method, "strictness": growth.strictness}
    return {
        "method": "degurba",
        "reference": "Degree of Urbanisation (Dijkstra and Poelman; GHSL 2023), adapted to Kontur H3 r8 cells",
        "centre_density_min_per_sqkm_land": growth.centre_density_min,
        "builtup_share_min": growth.builtup_share_min,
        "centre_min_population": growth.centre_min_population,
        "gap_fill_min_neighbors_of_6": growth.gap_fill_min_neighbors,
        "max_hole_area_sqkm": growth.max_hole_area_sqkm,
        "cluster_density_min_per_sqkm_land": growth.cluster_density_min,
        "cluster_min_population": growth.cluster_min_population,
        "study_area": "urban centre containing the configured city-centre point"
        if not growth.keep_other_centres
        else "all urban centres in the envelope",
        "resolution_caveat": "H3 r8 cells (~0.74 sq km) are smaller than the 1 sq km DEGURBA grid",
    }


def run_urban_area_pipeline(
    config: CityConfig,
    *,
    query_missing: bool = False,
    write_outputs: bool = True,
) -> dict[str, Any]:
    """Run the robust urban-area detection pipeline for one city."""
    city_boundary, metric_crs = load_city_boundary(config)
    population_cells = load_population_cells(config, city_boundary, metric_crs)
    validate_population_cells(population_cells).raise_for_errors()

    try:
        buildings = load_buildings(config)
    except FileNotFoundError:
        if not query_missing:
            raise
        buildings = _query_osm_buildings(city_boundary, metric_crs)
    validate_buildings(buildings, expected_mode=config.building_mode).raise_for_errors()

    queried_roads = False
    try:
        roads = load_roads(config)
    except FileNotFoundError:
        if not query_missing:
            raise
        pbf_path = city_pbf_path(config.resolved_slug())
        if pbf_path.exists():
            roads = load_pbf_roads(pbf_path, city_slug=config.resolved_slug())
        else:
            roads = _query_osm_roads(city_boundary, metric_crs, config.road_tags)
        queried_roads = True
    roads = normalize_roads(
        roads,
        metric_crs,
        local_values=config.local_road_values,
        major_values=config.major_road_values,
    )
    validate_roads(roads).raise_for_errors()
    if queried_roads:
        config.road_gpkg.parent.mkdir(parents=True, exist_ok=True)
        roads.to_file(config.road_gpkg, layer=config.resolved_road_layer(), driver="GPKG")

    buildings_agg = aggregate_buildings_to_cells(
        buildings,
        population_cells,
        building_mode=config.building_mode,
    )
    analysis_cells = aggregate_roads_to_cells(
        roads,
        buildings_agg.cells,
        local_values=config.local_road_values,
        major_values=config.major_road_values,
    )
    water_mask = None
    if config.water_mask.enabled:
        water_mask = load_or_query_water_mask(
            config,
            city_boundary,
            metric_crs,
            query_missing=query_missing,
        )
        # Annotate land area before metrics so densities use land, not water.
        analysis_cells = apply_water_exclusion(
            analysis_cells,
            water_mask,
            min_land_fraction=config.water_mask.min_land_fraction,
        )
    analysis_cells = add_urban_metrics(analysis_cells, building_mode=buildings_agg.mode)
    validate_analysis_cells(analysis_cells).raise_for_errors()
    analysis_adjacency = build_adjacency_graph(analysis_cells, id_col="cell_id", mode="edge")
    centre_point = _centre_point(config, metric_crs)

    analysis_cells, urban_cells = detect_urban_cells(
        analysis_cells,
        config.growth,
        adjacency=analysis_adjacency,
        centre_point=centre_point,
    )

    if config.refinement.enabled and not urban_cells.empty:
        urban_cells = refine_urban_edges(
            urban_cells,
            analysis_cells,
            buildings,
            roads,
            building_mode=buildings_agg.mode,
            subdivision_factor=config.refinement.subdivision_factor,
            min_building_area_sqm=config.refinement.min_building_area_sqm,
            min_road_density_km_per_sqkm=config.refinement.min_road_density_km_per_sqkm,
            require_local_presence=config.refinement.require_local_presence,
            local_road_values=config.local_road_values,
            major_road_values=config.major_road_values,
            adjacency=analysis_adjacency,
            **component_retention_kwargs(config.growth),
        )

    if water_mask is not None and not urban_cells.empty:
        urban_cells = clip_cells_to_land(urban_cells, water_mask)
    urban_cells = add_absolute_metrics(
        urban_cells.drop(columns=NORMALISED_SCORE_COLUMNS, errors="ignore"),
        building_mode=buildings_agg.mode,
    )

    if "Name" not in urban_cells.columns:
        urban_cells["Name"] = config.city_name
    if "is_refined" not in urban_cells.columns:
        urban_cells["is_refined"] = False
    if "parent_cell_id" not in urban_cells.columns:
        urban_cells["parent_cell_id"] = urban_cells["cell_id"]

    urban_hull = hull_from_cells(urban_cells)
    normalized_roads = normalize_roads(
        roads,
        metric_crs,
        local_values=config.local_road_values,
        major_values=config.major_road_values,
    )
    source_road_length_m = float(normalized_roads["length_m"].sum()) if "length_m" in normalized_roads else None
    if config.refinement.enabled:
        urban_adjacency = build_adjacency_graph(urban_cells, id_col="cell_id", mode="edge")
    else:
        urban_ids = set(urban_cells["cell_id"])
        urban_adjacency = {
            cell_id: {neighbor for neighbor in analysis_adjacency.get(cell_id, set()) if neighbor in urban_ids}
            for cell_id in urban_ids
        }
    validation_report = build_validation_report(
        population_cells=population_cells,
        buildings=buildings,
        roads=roads,
        analysis_cells=analysis_cells,
        urban_cells=urban_cells,
        building_mode=buildings_agg.mode,
        source_road_length_m=source_road_length_m,
        building_warnings=buildings_agg.warnings,
        growth=config.growth,
        centre_point=centre_point,
        analysis_adjacency=analysis_adjacency,
        urban_adjacency=urban_adjacency,
    )
    provenance = {
        "city_name": config.city_name,
        "city_slug": config.resolved_slug(),
        "metric_crs": metric_crs,
        "urban_area_type": "morphological_built_up_area",
        "station_filtering_note": "Use catchment population/intersection logic, not station point-in-polygon tests",
        "building_mode": buildings_agg.mode,
        "building_source_geometry_types": buildings_agg.source_geometry_types,
        "building_warnings": buildings_agg.warnings,
        "road_signal": {
            "scoring_column": "local_road_density_km_per_sqkm",
            "major_transport_roads_diagnostic_only": config.major_road_values,
            "local_urban_fabric_roads": config.local_road_values,
        },
        "water_mask": {
            "enabled": config.water_mask.enabled,
            "layer": config.water_mask.resolved_layer(config.resolved_slug()) if config.water_mask.enabled else None,
            "min_land_fraction": config.water_mask.min_land_fraction,
            "densities_on_land_area": bool(config.water_mask.enabled),
            "published_geometry_clipped": bool(config.water_mask.enabled),
            "population_scaled_by_clip": False,
        },
        "urban_detection": _detection_provenance(config),
        "run_started_at_utc": datetime.now(timezone.utc).isoformat(),
        "config": _config_to_jsonable(config),
        "outputs": {
            "analysis_layer": f"population_density_with_buildings_roads_{config.resolved_slug()}",
            "urban_cells_method": "growing_refined" if config.refinement.enabled else "growing",
        },
    }

    outputs = {}
    if write_outputs:
        outputs = save_pipeline_outputs(
            config=config,
            analysis_cells=analysis_cells,
            urban_cells=urban_cells,
            urban_hull=urban_hull,
            validation_report=validation_report,
            provenance=provenance,
        )

    return {
        "config": config,
        "metric_crs": metric_crs,
        "building_mode": buildings_agg.mode,
        "analysis_cells": analysis_cells,
        "urban_cells": urban_cells,
        "urban_hull": urban_hull,
        "validation_report": validation_report,
        "provenance": provenance,
        "outputs": outputs,
    }
