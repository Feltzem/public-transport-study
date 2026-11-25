"""Validation reports for urban-area outputs."""

from __future__ import annotations

import math
from typing import Any

import geopandas as gpd
import pandas as pd
from shapely.geometry import Polygon

from .geometry import build_adjacency_graph, component_labels, clean_union
from .schemas import (
    validate_analysis_cells,
    validate_buildings,
    validate_population_cells,
    validate_roads,
)


def _series_summary(series: pd.Series) -> dict[str, float | None]:
    if series.empty:
        return {"min": None, "p05": None, "median": None, "p95": None, "max": None, "mean": None}
    numeric = pd.to_numeric(series, errors="coerce").dropna()
    if numeric.empty:
        return {"min": None, "p05": None, "median": None, "p95": None, "max": None, "mean": None}
    return {
        "min": float(numeric.min()),
        "p05": float(numeric.quantile(0.05)),
        "median": float(numeric.median()),
        "p95": float(numeric.quantile(0.95)),
        "max": float(numeric.max()),
        "mean": float(numeric.mean()),
    }


def _hole_stats(geom) -> dict[str, float | int]:
    if geom is None or geom.is_empty:
        return {"hole_count": 0, "hole_area_sqkm": 0.0}
    polygons = list(geom.geoms) if geom.geom_type == "MultiPolygon" else [geom] if geom.geom_type == "Polygon" else []
    hole_count = 0
    hole_area = 0.0
    for poly in polygons:
        hole_count += len(poly.interiors)
        for ring in poly.interiors:
            hole_area += abs(Polygon(ring).area)
    return {"hole_count": int(hole_count), "hole_area_sqkm": float(hole_area / 1_000_000)}


def component_summary(
    cells: gpd.GeoDataFrame,
    adjacency: dict[int, set[int]] | None = None,
) -> dict[str, Any]:
    if cells.empty:
        return {"component_count": 0, "component_areas_sqkm": []}
    if adjacency is None:
        adjacency = build_adjacency_graph(cells, id_col="cell_id", mode="edge")
    work = cells.copy()
    work["component"] = component_labels(work, adjacency, id_col="cell_id")
    areas = (work.groupby("component").geometry.apply(lambda s: s.area.sum()) / 1_000_000).sort_values(ascending=False)
    return {
        "component_count": int(len(areas)),
        "component_areas_sqkm": [float(v) for v in areas.to_list()],
    }


def sensitivity_summary(
    analysis_cells: gpd.GeoDataFrame,
    growth=None,
    adjacency: dict[int, set[int]] | None = None,
    centre_point=None,
) -> dict[str, Any]:
    """Run unrefined sensitivity checks for validation/provenance.

    DEGURBA runs vary the urban-centre density threshold; the legacy score
    method varies its strictness preset.
    """
    if analysis_cells.empty:
        return {}
    from dataclasses import replace

    from .config import GrowthConfig
    from .growth import detect_urban_cells

    growth = growth or GrowthConfig()
    if adjacency is None:
        adjacency = build_adjacency_graph(analysis_cells, id_col="cell_id", mode="edge")
    if growth.method == "degurba":
        variants = {
            f"centre_density_{int(value)}": replace(growth, centre_density_min=value)
            for value in [1200.0, 1500.0, 2000.0]
        }
    else:
        variants = {strictness: replace(growth, strictness=strictness) for strictness in ["strict", "medium", "loose"]}
    summary: dict[str, Any] = {}
    for name, variant in variants.items():
        try:
            _, cells = detect_urban_cells(analysis_cells, variant, adjacency=adjacency, centre_point=centre_point)
            pop = float(cells["population"].sum()) if "population" in cells else 0.0
            area = float(cells.geometry.area.sum() / 1_000_000) if not cells.empty else 0.0
            summary[name] = {
                "urban_cell_count": int(len(cells)),
                "urban_population": pop,
                "urban_area_sqkm": area,
                "components": component_summary(cells, adjacency=adjacency),
            }
        except Exception as exc:
            summary[name] = {"error": str(exc)}
    return summary


def _degurba_class_summary(analysis_cells: gpd.GeoDataFrame) -> dict[str, Any]:
    if "degurba_class" not in analysis_cells:
        return {}
    grouped = analysis_cells.groupby("degurba_class")
    return {
        str(name): {
            "cell_count": int(len(group)),
            "population": float(group["population"].sum()),
            "area_sqkm": float(group.geometry.area.sum() / 1_000_000),
        }
        for name, group in grouped
    }


def build_validation_report(
    *,
    population_cells: gpd.GeoDataFrame,
    buildings: gpd.GeoDataFrame,
    roads: gpd.GeoDataFrame,
    analysis_cells: gpd.GeoDataFrame,
    urban_cells: gpd.GeoDataFrame,
    building_mode: str,
    source_road_length_m: float | None = None,
    building_warnings: list[str] | None = None,
    growth=None,
    centre_point=None,
    analysis_adjacency: dict[int, set[int]] | None = None,
    urban_adjacency: dict[int, set[int]] | None = None,
) -> dict[str, Any]:
    unioned = clean_union(urban_cells)
    total_pop = float(analysis_cells["population"].sum()) if "population" in analysis_cells else math.nan
    urban_pop = float(urban_cells["population"].sum()) if "population" in urban_cells else math.nan
    urban_area_sqkm = float(urban_cells.geometry.area.sum() / 1_000_000) if not urban_cells.empty else 0.0
    analysis_area_sqkm = float(analysis_cells.geometry.area.sum() / 1_000_000) if not analysis_cells.empty else 0.0
    agg_road_length_m = float(analysis_cells.get("total_road_length_m", pd.Series(dtype=float)).sum())

    report: dict[str, Any] = {
        "contracts": {
            "population_cells": validate_population_cells(population_cells).to_dict(),
            "buildings": validate_buildings(buildings, expected_mode=building_mode).to_dict(),
            "roads": validate_roads(roads).to_dict(),
            "analysis_cells": validate_analysis_cells(analysis_cells).to_dict(),
        },
        "summary": {
            "building_mode": building_mode,
            "analysis_cell_count": int(len(analysis_cells)),
            "urban_cell_count": int(len(urban_cells)),
            "total_population": total_pop,
            "urban_population": urban_pop,
            "urban_population_share": float(urban_pop / total_pop) if total_pop else None,
            "analysis_area_sqkm": analysis_area_sqkm,
            "urban_area_sqkm": urban_area_sqkm,
            "urban_area_share": float(urban_area_sqkm / analysis_area_sqkm) if analysis_area_sqkm else None,
        },
        "components": component_summary(urban_cells, adjacency=urban_adjacency),
        "holes": _hole_stats(unioned),
        "distributions": {
            "building_coverage": _series_summary(analysis_cells.get("building_coverage", pd.Series(dtype=float))),
            "built_up_coverage": _series_summary(analysis_cells.get("built_up_coverage", pd.Series(dtype=float))),
            "population_density": _series_summary(analysis_cells.get("population_density", pd.Series(dtype=float))),
            "building_point_density_per_sqkm": _series_summary(
                analysis_cells.get("building_point_density_per_sqkm", pd.Series(dtype=float))
            ),
            "road_density_km_per_sqkm": _series_summary(
                analysis_cells.get("road_density_km_per_sqkm", pd.Series(dtype=float))
            ),
            "local_road_density_km_per_sqkm": _series_summary(
                analysis_cells.get("local_road_density_km_per_sqkm", pd.Series(dtype=float))
            ),
            "major_road_density_km_per_sqkm": _series_summary(
                analysis_cells.get("major_road_density_km_per_sqkm", pd.Series(dtype=float))
            ),
            "urban_score_base": _series_summary(analysis_cells.get("urban_score_base", pd.Series(dtype=float))),
        },
        "degurba_classes": _degurba_class_summary(analysis_cells),
        "sensitivity": sensitivity_summary(
            analysis_cells,
            growth=growth,
            adjacency=analysis_adjacency,
            centre_point=centre_point,
        ),
        "road_length_check": {
            "source_road_length_km": float(source_road_length_m / 1000) if source_road_length_m is not None else None,
            "aggregated_road_length_km": float(agg_road_length_m / 1000),
            "aggregated_to_source_ratio": float(agg_road_length_m / source_road_length_m)
            if source_road_length_m
            else None,
        },
    }
    if report["summary"]["building_mode"] == "footprint":
        total_building_area = float(analysis_cells.get("total_building_area_sqm", pd.Series(dtype=float)).sum())
        if total_building_area == 0:
            report["contracts"]["analysis_cells"]["warnings"].append("Footprint mode produced zero total building area")
    for warning in building_warnings or []:
        report["contracts"]["buildings"]["warnings"].append(warning)
    return report
