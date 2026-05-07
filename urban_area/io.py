"""Input/output helpers for urban-area processing."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import json
import math

import fiona
import geopandas as gpd
import pandas as pd
from shapely.ops import unary_union

from .config import CityConfig
from .geometry import make_valid_2d


def list_layers(gpkg_path: str | Path) -> list[str]:
    try:
        return list(fiona.listlayers(str(gpkg_path)))
    except Exception:
        return []


def require_layer(gpkg_path: str | Path, layer: str) -> None:
    layers = list_layers(gpkg_path)
    if layer not in layers:
        raise FileNotFoundError(f"Layer {layer!r} not found in {gpkg_path}. Available layers: {layers}")


def load_city_boundary(config: CityConfig) -> tuple[gpd.GeoDataFrame, str]:
    """Load the configured city boundary in the configured/inferred metric CRS."""
    require_layer(config.boundaries_path, config.boundaries_layer)
    cities = gpd.read_file(config.boundaries_path, layer=config.boundaries_layer)
    city = cities[cities["Name"] == config.city_name].copy()
    if city.empty:
        raise ValueError(f"City {config.city_name!r} not found in {config.boundaries_path}/{config.boundaries_layer}")
    metric_crs = config.resolved_metric_crs()
    if metric_crs is None:
        metric_crs = city.estimate_utm_crs().to_string()
    city_metric = city.to_crs(metric_crs)
    if config.analysis_envelope_buffer_m:
        city_metric = city_metric.copy()
        city_metric["geometry"] = city_metric.geometry.buffer(config.analysis_envelope_buffer_m)
    return city_metric, metric_crs


def _is_extensive_numeric_column(column: str) -> bool:
    """Heuristic for attributes that should scale with clipped cell area."""
    lower = column.lower()
    if lower.startswith("__"):
        return False
    intensive_markers = ["density", "rate", "ratio", "share", "percent", "pct", "mean", "median", "avg"]
    if any(marker in lower for marker in intensive_markers):
        return False
    extensive_markers = ["population", "pop", "count", "persons", "people", "household", "workers", "jobs"]
    return any(marker in lower for marker in extensive_markers)


def _has_finite_bounds(geom) -> bool:
    if geom is None or geom.is_empty:
        return False
    try:
        return all(math.isfinite(value) for value in geom.bounds)
    except Exception:
        return False


def load_population_cells(config: CityConfig, city_boundary_metric: gpd.GeoDataFrame, metric_crs: str) -> gpd.GeoDataFrame:
    """Load and clip population cells, then project them before metrics."""
    require_layer(config.population_gpkg, config.population_layer)
    population = gpd.read_file(config.population_gpkg, layer=config.population_layer)
    population = population.to_crs(metric_crs)
    population = population[population.geometry.notna() & ~population.geometry.is_empty].copy()
    population = population[population.geometry.apply(_has_finite_bounds)].copy()
    population.geometry = population.geometry.apply(make_valid_2d)
    population = population[population.geometry.notna() & ~population.geometry.is_empty & population.geometry.is_valid].copy()
    population["__orig_area_sqm"] = population.geometry.area
    population["__orig_population"] = population["population"]
    numeric_cols = [
        col
        for col in population.select_dtypes(include="number").columns
        if col not in {"__orig_area_sqm", "__orig_population"}
    ]
    extensive_cols = [col for col in numeric_cols if _is_extensive_numeric_column(col)]
    boundary = city_boundary_metric.to_crs(metric_crs)
    boundary = boundary[boundary.geometry.notna() & ~boundary.geometry.is_empty].copy()
    boundary = boundary[boundary.geometry.apply(_has_finite_bounds)].copy()
    boundary.geometry = boundary.geometry.apply(make_valid_2d)
    boundary = boundary[boundary.geometry.notna() & ~boundary.geometry.is_empty & boundary.geometry.is_valid].copy()
    clipped = gpd.overlay(population, boundary[["Name", "geometry"]], how="intersection")
    clipped = clipped[clipped.geometry.notna() & ~clipped.geometry.is_empty].copy()
    clipped = clipped.reset_index(drop=True)
    if "Name" not in clipped.columns:
        for candidate in ["Name_2", "Name_right", "Name_boundary"]:
            if candidate in clipped.columns:
                clipped["Name"] = clipped[candidate]
                break
    clipped["clipped_area_sqm"] = clipped.geometry.area
    clipped["population_clip_fraction"] = (
        clipped["clipped_area_sqm"] / clipped["__orig_area_sqm"].where(clipped["__orig_area_sqm"] != 0)
    ).clip(0, 1).fillna(0)
    for col in extensive_cols:
        if col in clipped.columns:
            clipped[col] = clipped[col] * clipped["population_clip_fraction"]
    clipped["source_population"] = clipped["__orig_population"]
    clipped["population"] = clipped["__orig_population"] * clipped["population_clip_fraction"]
    clipped["cell_id"] = range(len(clipped))
    clipped["cell_area_sqm"] = clipped.geometry.area
    clipped["cell_area_sqkm"] = clipped["cell_area_sqm"] / 1_000_000
    clipped = clipped.drop(columns=["__orig_area_sqm", "__orig_population"], errors="ignore")
    return clipped


def load_optional_layer(gpkg_path: str | Path, layer: str) -> gpd.GeoDataFrame | None:
    if not Path(gpkg_path).exists() or layer not in list_layers(gpkg_path):
        return None
    return gpd.read_file(gpkg_path, layer=layer)


def load_buildings(config: CityConfig) -> gpd.GeoDataFrame:
    layer = config.resolved_building_layer()
    buildings = load_optional_layer(config.building_gpkg, layer)
    if buildings is None:
        raise FileNotFoundError(f"Building layer {layer!r} not found in {config.building_gpkg}")
    return buildings


def load_roads(config: CityConfig) -> gpd.GeoDataFrame:
    layer = config.resolved_road_layer()
    roads = load_optional_layer(config.road_gpkg, layer)
    if roads is None:
        raise FileNotFoundError(f"Road layer {layer!r} not found in {config.road_gpkg}")
    return roads


def save_pipeline_outputs(
    *,
    config: CityConfig,
    analysis_cells: gpd.GeoDataFrame,
    urban_cells: gpd.GeoDataFrame,
    urban_hull: gpd.GeoDataFrame,
    validation_report: dict[str, object],
    provenance: dict[str, object],
) -> dict[str, str]:
    """Save analysis, urban cells, hull, and provenance sidecars."""
    data_dir = config.data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    slug = config.resolved_slug()
    method = "growing_refined" if config.refinement.enabled else "growing"
    analysis_layer = f"population_density_with_buildings_roads_{slug}"
    cells_layer = f"urban_cells_{slug}_{method}"
    hull_layer = f"urban_area_{slug}_{method}"
    analysis_gpkg = config.population_gpkg
    urban_gpkg = config.urban_areas_gpkg
    urban_cells_geojson = data_dir / f"urban_cells_{slug}_{method}.geojson"
    provenance_dir = data_dir / "provenance"
    provenance_dir.mkdir(parents=True, exist_ok=True)
    provenance_path = provenance_dir / f"{slug}_{method}_provenance.json"
    validation_path = provenance_dir / f"{slug}_{method}_validation.json"

    analysis_cells.to_file(analysis_gpkg, layer=analysis_layer, driver="GPKG")
    urban_cells.to_crs("EPSG:4326").to_file(urban_cells_geojson, driver="GeoJSON")
    urban_cells.to_crs("EPSG:4326").to_file(urban_gpkg, layer=cells_layer, driver="GPKG")
    urban_hull.to_crs("EPSG:4326").to_file(urban_gpkg, layer=hull_layer, driver="GPKG")

    provenance_payload = {
        "written_at_utc": datetime.now(timezone.utc).isoformat(),
        **provenance,
    }
    provenance_path.write_text(json.dumps(provenance_payload, indent=2, sort_keys=True), encoding="utf-8")
    validation_path.write_text(json.dumps(validation_report, indent=2, sort_keys=True), encoding="utf-8")
    return {
        "analysis_layer": f"{analysis_gpkg}/{analysis_layer}",
        "urban_cells_layer": f"{urban_gpkg}/{cells_layer}",
        "urban_hull_layer": f"{urban_gpkg}/{hull_layer}",
        "urban_cells_geojson": str(urban_cells_geojson),
        "provenance_json": str(provenance_path),
        "validation_json": str(validation_path),
    }


def hull_from_cells(urban_cells: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    unioned = unary_union(urban_cells.geometry)
    return gpd.GeoDataFrame(
        {
            "city_name": [urban_cells["Name"].dropna().iloc[0] if "Name" in urban_cells.columns and not urban_cells["Name"].dropna().empty else None],
            "cell_count": [len(urban_cells)],
            "total_population": [float(urban_cells["population"].sum()) if "population" in urban_cells.columns else None],
            "area_sqkm": [float(unioned.area / 1_000_000)],
            "geometry": [unioned],
        },
        crs=urban_cells.crs,
    )
