"""Derived urban indicator metrics."""

from __future__ import annotations

import numpy as np
import pandas as pd


def robust_minmax(series: pd.Series, lower: float = 0.05, upper: float = 0.95) -> pd.Series:
    lo = series.quantile(lower)
    hi = series.quantile(upper)
    denom = hi - lo
    if not np.isfinite(denom) or denom == 0:
        return pd.Series(0.0, index=series.index)
    return ((series - lo) / denom).clip(0, 1).fillna(0)


def add_urban_metrics(cells, *, building_mode: str) -> object:
    """Add normalized indicators and base urban score."""
    result = cells.copy()
    result["cell_area_sqm"] = result.geometry.area
    result["cell_area_sqkm"] = result["cell_area_sqm"] / 1_000_000
    result["population_density"] = (
        result["population"] / result["cell_area_sqkm"].replace(0, np.nan)
    ).replace([np.inf, -np.inf], 0).fillna(0)
    if "building_coverage" not in result.columns:
        result["building_coverage"] = (
            result.get("total_building_area_sqm", 0) / result["cell_area_sqm"]
        ).clip(0, 1).fillna(0)
    if "built_up_coverage" not in result.columns:
        result["built_up_coverage"] = result["building_coverage"]
    if "building_area_sqm" not in result.columns and "total_building_area_sqm" in result.columns:
        result["building_area_sqm"] = result["total_building_area_sqm"]
    if "building_point_density_per_sqkm" not in result.columns:
        result["building_point_density_per_sqkm"] = 0.0
    if "building_signal_type" not in result.columns:
        result["building_signal_type"] = "footprint_area" if building_mode == "footprint" else "point_fallback"
    if "building_signal_confidence" not in result.columns:
        result["building_signal_confidence"] = 1.0 if building_mode == "footprint" else 0.5

    result["pop_norm"] = robust_minmax(result["population_density"])
    result["bldg_cov_norm"] = robust_minmax(result["built_up_coverage"])
    result["bldg_point_norm"] = robust_minmax(result["building_point_density_per_sqkm"])
    road_signal_col = (
        "local_road_density_km_per_sqkm"
        if "local_road_density_km_per_sqkm" in result.columns
        else "road_density_km_per_sqkm"
    )
    if road_signal_col in result.columns:
        result["road_norm"] = robust_minmax(result[road_signal_col])
    else:
        result["road_norm"] = 0.0

    if building_mode == "point":
        result["building_signal_norm"] = (result["bldg_point_norm"] * result["building_signal_confidence"]).clip(0, 1)
    else:
        result["building_signal_norm"] = result["bldg_cov_norm"]

    result["urban_score_base"] = (
        0.52 * result["pop_norm"] + 0.43 * result["building_signal_norm"] + 0.05 * result["road_norm"]
    )
    result["building_mode"] = building_mode
    return result
