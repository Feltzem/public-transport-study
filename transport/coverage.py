"""Area/building-weighted public-transport coverage metrics."""

from __future__ import annotations

import geopandas as gpd

from urban_area.catchment import population_within_catchments


def calculate_coverage(
    urban_cells: gpd.GeoDataFrame,
    catchments: gpd.GeoDataFrame,
    *,
    buildings: gpd.GeoDataFrame | None = None,
) -> tuple[dict[str, float | None], gpd.GeoDataFrame]:
    """Return weighted coverage summary and per-cell coverage fields."""
    cells = urban_cells.copy().reset_index(drop=True)
    if catchments.empty:
        cells["covered_pop"] = 0.0
        cells["covered_frac"] = 0.0
        total = float(cells["population"].sum())
        return {
            "urban_population": total,
            "covered_population": 0.0,
            "pct_within_15min": 0.0 if total else None,
            "legacy_centroid_population": 0.0,
            "legacy_centroid_pct": 0.0 if total else None,
        }, cells
    weighted_population, intersections = population_within_catchments(
        cells,
        catchments,
        method="auto",
        buildings=buildings,
        dissolve=True,
    )
    centroid_population, _ = population_within_catchments(cells, catchments, method="centroid", dissolve=True)
    covered_by_cell = (
        intersections.groupby("_cell_id")["covered_population"].sum()
        if not intersections.empty and "_cell_id" in intersections
        else {}
    )
    cells["covered_pop"] = cells.index.to_series().map(covered_by_cell).fillna(0.0)
    cells["covered_frac"] = (cells["covered_pop"] / cells["population"].where(cells["population"] > 0)).clip(0, 1).fillna(0.0)
    total = float(cells["population"].sum())
    summary = {
        "urban_population": total,
        "covered_population": float(weighted_population),
        "pct_within_15min": float(100 * weighted_population / total) if total else None,
        "legacy_centroid_population": float(centroid_population),
        "legacy_centroid_pct": float(100 * centroid_population / total) if total else None,
    }
    return summary, cells


def frequency_eligible_catchments(
    catchments: gpd.GeoDataFrame,
    stations: gpd.GeoDataFrame,
    *,
    minimum_departures_per_hour: float = 6.0,
) -> gpd.GeoDataFrame:
    """Catchments of stations at or above the busiest-direction frequency threshold."""
    eligible = set(stations.loc[stations["departures_per_hr"] >= minimum_departures_per_hour, "station_id"])
    return catchments[catchments["station_id"].isin(eligible)].copy()


def polygon_coverage(
    urban_cells: gpd.GeoDataFrame,
    polygon,
    *,
    buildings: gpd.GeoDataFrame | None = None,
) -> tuple[dict[str, float | None], gpd.GeoDataFrame]:
    """Coverage of one dissolved catchment polygon (None means no coverage)."""
    frame = gpd.GeoDataFrame(
        {"station_id": [0] if polygon is not None and not polygon.is_empty else []},
        geometry=[polygon] if polygon is not None and not polygon.is_empty else [],
        crs=urban_cells.crs,
    )
    return calculate_coverage(urban_cells, frame, buildings=buildings)
