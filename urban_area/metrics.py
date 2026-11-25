"""Derived urban indicator metrics."""

from __future__ import annotations

import numpy as np
import pandas as pd


NORMALISED_SCORE_COLUMNS = [
    "pop_norm",
    "bldg_cov_norm",
    "bldg_point_norm",
    "road_norm",
    "building_signal_norm",
    "urban_score_base",
]


def robust_minmax(series: pd.Series, lower: float = 0.05, upper: float = 0.95) -> pd.Series:
    lo = series.quantile(lower)
    hi = series.quantile(upper)
    denom = hi - lo
    if not np.isfinite(denom) or denom == 0:
        return pd.Series(0.0, index=series.index)
    return ((series - lo) / denom).clip(0, 1).fillna(0)


def add_absolute_metrics(cells, *, building_mode: str) -> object:
    """Add area, density, and coverage fields that do not depend on other cells.

    When ``land_area_sqm`` is present (set by the water mask), densities and
    coverage use land area, because residents and buildings sit on land.
    """
    result = cells.copy()
    result["cell_area_sqm"] = result.geometry.area
    result["cell_area_sqkm"] = result["cell_area_sqm"] / 1_000_000
    if "land_area_sqm" in result.columns:
        land_area_sqm = result["land_area_sqm"].clip(lower=0).fillna(result["cell_area_sqm"])
    else:
        land_area_sqm = result["cell_area_sqm"]
    land_area_sqkm = land_area_sqm / 1_000_000
    result["population_density"] = (
        result["population"] / land_area_sqkm.replace(0, np.nan)
    ).replace([np.inf, -np.inf], 0).fillna(0)
    if "total_building_area_sqm" in result.columns:
        result["building_coverage"] = (
            result["total_building_area_sqm"] / land_area_sqm.replace(0, np.nan)
        ).clip(0, 1).fillna(0)
    elif "building_coverage" not in result.columns:
        result["building_coverage"] = 0.0
    result["built_up_coverage"] = result["building_coverage"]
    if "building_area_sqm" not in result.columns and "total_building_area_sqm" in result.columns:
        result["building_area_sqm"] = result["total_building_area_sqm"]
    if "building_point_density_per_sqkm" not in result.columns:
        result["building_point_density_per_sqkm"] = 0.0
    if "building_signal_type" not in result.columns:
        result["building_signal_type"] = "footprint_area" if building_mode == "footprint" else "point_fallback"
    if "building_signal_confidence" not in result.columns:
        result["building_signal_confidence"] = 1.0 if building_mode == "footprint" else 0.5
    result["building_mode"] = building_mode
    return result


def add_urban_metrics(cells, *, building_mode: str) -> object:
    """Add absolute metrics plus percentile-normalised indicators and base score.

    The normalised fields are relative to the supplied cells, so they are only
    meaningful on the full analysis envelope and are diagnostic for the
    DEGURBA method.
    """
    result = add_absolute_metrics(cells, building_mode=building_mode)
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
    if "is_water" in result.columns:
        water = result["is_water"].fillna(False).astype(bool)
        for column in NORMALISED_SCORE_COLUMNS:
            result.loc[water, column] = 0.0
    return result
