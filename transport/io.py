"""Shared input/output helpers for transport analysis."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

import fiona
import geopandas as gpd

from .config import TransportConfig


def list_layers(path: str | Path) -> list[str]:
    try:
        return list(fiona.listlayers(path))
    except Exception:
        return []


def load_required_layer(path: str | Path, layer: str, *, crs: str | None = None) -> gpd.GeoDataFrame:
    if layer not in list_layers(path):
        raise FileNotFoundError(f"Layer {layer!r} not found in {path}; available: {list_layers(path)}")
    result = gpd.read_file(path, layer=layer)
    return result.to_crs(crs) if crs and result.crs != crs else result


def load_transport_inputs(config: TransportConfig) -> dict[str, gpd.GeoDataFrame | None]:
    crs = config.resolved_metric_crs()
    urban_cells = load_required_layer(config.urban_areas_gpkg, config.urban_cells_layer(), crs=crs)
    analysis_cells = load_required_layer(config.population_gpkg, config.analysis_cells_layer(), crs=crs)
    boundaries = load_required_layer(config.boundaries_path, config.boundaries_layer, crs=crs)
    boundary = boundaries[boundaries["Name"] == config.city_name].copy()
    if boundary.empty:
        raise ValueError(f"Boundary for {config.city_name!r} not found")
    buildings = None
    layer = config.resolved_building_layer()
    if layer in list_layers(config.building_gpkg):
        buildings = gpd.read_file(config.building_gpkg, layer=layer).to_crs(crs)
    return {
        "urban_cells": urban_cells,
        "analysis_cells": analysis_cells,
        "boundary": boundary,
        "buildings": buildings,
    }


def write_layer(gdf: gpd.GeoDataFrame, path: Path, layer: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    gdf.to_file(path, layer=layer, driver="GPKG", mode="w")
    return f"{path}/{layer}"


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    body = {"written_at_utc": datetime.now(timezone.utc).isoformat(), **payload}
    temporary.write_text(json.dumps(body, indent=2, sort_keys=True, default=str), encoding="utf-8")
    temporary.replace(path)
