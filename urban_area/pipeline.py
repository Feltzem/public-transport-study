"""End-to-end urban-area detection pipeline."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import geopandas as gpd
from shapely.geometry import Polygon, MultiPolygon

from .buildings import aggregate_buildings_to_cells
from .config import CityConfig
from .growth import run_growing_method
from .io import (
    hull_from_cells,
    load_buildings,
    load_city_boundary,
    load_population_cells,
    load_roads,
    save_pipeline_outputs,
)
from .metrics import add_urban_metrics
from .refinement import refine_urban_edges
from .roads import aggregate_roads_to_cells, normalize_roads
from .schemas import validate_analysis_cells, validate_buildings, validate_population_cells, validate_roads
from .validation import build_validation_report


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
    roads = ox.features_from_polygon(query_polygon, tags=road_tags)
    if roads.empty:
        return gpd.GeoDataFrame(columns=["geometry"], geometry="geometry", crs=metric_crs)
    return roads.to_crs(metric_crs)


def _config_to_jsonable(config: CityConfig) -> dict[str, Any]:
    data = asdict(config)
    for key, value in list(data.items()):
        if isinstance(value, Path):
            data[key] = str(value)
    return data


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

    try:
        roads = load_roads(config)
    except FileNotFoundError:
        if not query_missing:
            raise
        roads = _query_osm_roads(city_boundary, metric_crs, config.road_tags)
    validate_roads(roads).raise_for_errors()

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
    analysis_cells = add_urban_metrics(analysis_cells, building_mode=buildings_agg.mode)
    validate_analysis_cells(analysis_cells).raise_for_errors()

    urban_cells = run_growing_method(
        analysis_cells,
        urban_strictness=config.growth.strictness,
        water_guard=config.growth.water_guard,
        multi_seed=config.growth.use_multi_seed,
        min_seed_cluster_size=config.growth.min_seed_cluster_size,
        keep_largest_component=config.growth.keep_largest_component,
        min_component_population=config.growth.min_component_population,
        min_component_area_sqkm=config.growth.min_component_area_sqkm,
        min_component_built_up_area_sqm=config.growth.min_component_built_up_area_sqm,
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
            keep_largest_component=config.growth.keep_largest_component,
            min_component_population=config.growth.min_component_population,
            min_component_area_sqkm=config.growth.min_component_area_sqkm,
            min_component_built_up_area_sqm=config.growth.min_component_built_up_area_sqm,
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
    growth_kwargs = {
        "water_guard": config.growth.water_guard,
        "multi_seed": config.growth.use_multi_seed,
        "min_seed_cluster_size": config.growth.min_seed_cluster_size,
        "keep_largest_component": config.growth.keep_largest_component,
        "min_component_population": config.growth.min_component_population,
        "min_component_area_sqkm": config.growth.min_component_area_sqkm,
        "min_component_built_up_area_sqm": config.growth.min_component_built_up_area_sqm,
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
        growth_kwargs=growth_kwargs,
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
