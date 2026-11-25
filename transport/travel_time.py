"""r5py door-to-door travel-time and accessibility metrics."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
from shapely.geometry import Point


def _weighted_quantile(values: pd.Series, weights: pd.Series, quantile: float) -> float | None:
    """Weighted quantile where missing values (unreachable cells) rank last.

    Returns None when the quantile falls among unreachable cells.
    """
    values = values.astype(float).fillna(np.inf)
    valid = weights.notna() & (weights >= 0)
    if not valid.any() or float(weights[valid].sum()) == 0:
        return None
    order = np.argsort(values[valid].to_numpy())
    sorted_values = values[valid].to_numpy()[order]
    sorted_weights = weights[valid].to_numpy()[order]
    cutoff = quantile * sorted_weights.sum()
    position = min(int(np.searchsorted(np.cumsum(sorted_weights), cutoff, side="left")), len(sorted_values) - 1)
    result = float(sorted_values[position])
    return result if np.isfinite(result) else None


def cumulative_accessibility(
    matrix: pd.DataFrame,
    origins: pd.DataFrame,
    *,
    cutoffs_min: tuple[int, ...] = (30, 45),
) -> dict[str, float | None]:
    """Population-weighted share of urban residents reachable within each cutoff.

    For each origin cell, sum the population of destination cells reachable
    within the cutoff (unreachable pairs count as not reachable), divide by
    total urban population, then average across origins weighted by origin
    population.
    """
    population = origins.set_index("id")["population"].astype(float)
    total = float(population.sum())
    metrics: dict[str, float | None] = {}
    if total <= 0 or matrix.empty:
        return {f"access_pop_{cutoff}min_pct": None for cutoff in cutoffs_min}
    work = matrix[["from_id", "to_id", "travel_time"]].copy()
    work["destination_population"] = work["to_id"].map(population).fillna(0.0)
    for cutoff in cutoffs_min:
        reachable = work[work["travel_time"] <= cutoff].groupby("from_id")["destination_population"].sum()
        share = reachable.reindex(population.index, fill_value=0.0) / total
        metrics[f"access_pop_{cutoff}min_pct"] = float(100 * (share * population).sum() / total)
    return metrics


def _travel_time_matrix(r5py, network, origins, destinations, departure, window_hours=2):
    kwargs = {
        "transport_network": network,
        "origins": origins,
        "destinations": destinations,
        "departure": departure,
        "departure_time_window": timedelta(hours=window_hours),
        "transport_modes": [r5py.TransportMode.TRANSIT, r5py.TransportMode.WALK],
        "snap_to_network": True,
    }
    if hasattr(r5py, "TravelTimeMatrix"):
        return pd.DataFrame(r5py.TravelTimeMatrix(**kwargs))
    kwargs.pop("snap_to_network", None)
    return r5py.TravelTimeMatrixComputer(**kwargs).compute_travel_times()


def compute_travel_time_metrics(
    population_cells: gpd.GeoDataFrame,
    *,
    osm_pbf: Path,
    gtfs_paths: list[Path],
    centre_lon: float,
    centre_lat: float,
    service_date: date,
    start_hour: int = 7,
    window_hours: int = 2,
    all_to_all: bool = False,
) -> tuple[dict[str, float | None], pd.DataFrame, pd.DataFrame | None]:
    """Compute centre accessibility and optional all-cell travel times."""
    try:
        import r5py
    except ImportError as exc:
        raise RuntimeError("r5py is required for travel-time routing; install environment.yml and JDK 21") from exc
    if not osm_pbf.exists():
        raise FileNotFoundError(f"OSM PBF extract not found: {osm_pbf}")
    for path in gtfs_paths:
        if not path.exists():
            raise FileNotFoundError(f"GTFS feed not found: {path}")

    metric_cells = population_cells.copy().reset_index(drop=True)
    if not metric_cells.crs or not metric_cells.crs.is_projected:
        metric_cells = metric_cells.to_crs(metric_cells.estimate_utm_crs())
    centroids = gpd.GeoSeries(metric_cells.geometry.centroid, crs=metric_cells.crs).to_crs("EPSG:4326")
    origins = population_cells.to_crs("EPSG:4326").copy().reset_index(drop=True)
    origins["id"] = origins.get("cell_id", origins.index).astype(str)
    origins.geometry = centroids.to_numpy()
    origins = origins[["id", "population", "geometry"]]
    centre = gpd.GeoDataFrame({"id": ["centre"], "geometry": [Point(centre_lon, centre_lat)]}, crs="EPSG:4326")
    network = r5py.TransportNetwork(osm_pbf, gtfs_paths)
    departure = datetime.combine(service_date, time(start_hour, 0))
    to_centre = _travel_time_matrix(r5py, network, origins, centre, departure, window_hours)
    to_centre = to_centre.merge(origins[["id", "population"]], left_on="from_id", right_on="id", how="left")
    total_population = float(to_centre["population"].sum())
    within_30 = float(to_centre.loc[to_centre["travel_time"] <= 30, "population"].sum())
    within_45 = float(to_centre.loc[to_centre["travel_time"] <= 45, "population"].sum())

    origin_metric = origins.to_crs("EPSG:3035")
    centre_metric = centre.to_crs("EPSG:3035").geometry.iloc[0]
    distances_km = origin_metric.geometry.distance(centre_metric) / 1000
    distance_map = dict(zip(origins["id"], distances_km))
    to_centre["straight_line_km"] = to_centre["from_id"].map(distance_map)
    to_centre["straight_line_minutes_at_30kph"] = to_centre["straight_line_km"] / 30 * 60
    to_centre["pt_to_direct_speed_ratio"] = (
        to_centre["straight_line_minutes_at_30kph"] / to_centre["travel_time"].where(to_centre["travel_time"] > 0)
    )
    metrics = {
        "median_tt_centre_min": _weighted_quantile(to_centre["travel_time"], to_centre["population"], 0.5),
        "pct_centre_30min": 100 * within_30 / total_population if total_population else None,
        "pct_centre_45min": 100 * within_45 / total_population if total_population else None,
        "median_pt_to_direct_speed_ratio": _weighted_quantile(
            to_centre["pt_to_direct_speed_ratio"], to_centre["population"], 0.5
        ),
    }
    metrics["unreachable_centre_population_pct"] = (
        100 * float(to_centre.loc[to_centre["travel_time"].isna(), "population"].sum()) / total_population
        if total_population
        else None
    )
    all_cells = None
    if all_to_all:
        all_cells = _travel_time_matrix(r5py, network, origins, origins, departure, window_hours)
        metrics.update(cumulative_accessibility(all_cells, origins))
    return metrics, to_centre, all_cells
