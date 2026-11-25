"""Population catchment helpers for transit analysis."""

from __future__ import annotations

import geopandas as gpd
import pandas as pd
from shapely.ops import unary_union

from .geometry import vectorized_overlay_measure
from .schemas import POLYGON_TYPES


def _building_area_in_geometries(buildings: gpd.GeoDataFrame, geometries: gpd.GeoDataFrame, id_col: str) -> pd.Series:
    geometries_work = geometries[[id_col, "geometry"]].copy().reset_index(drop=True)
    geometries_work["_geometry_row_id"] = geometries_work.index
    weights_by_row = pd.Series(0.0, index=geometries_work["_geometry_row_id"])
    if buildings is None or buildings.empty or geometries.empty:
        return weights_by_row.groupby(geometries_work[id_col]).sum()
    buildings_work = buildings.to_crs(geometries.crs) if buildings.crs != geometries.crs else buildings.copy()
    footprints = buildings_work[
        buildings_work.geometry.notna() & buildings_work.geometry.is_valid & buildings_work.geom_type.isin(POLYGON_TYPES)
    ].copy()
    if footprints.empty:
        return weights_by_row.groupby(geometries_work[id_col]).sum()
    footprints = footprints.reset_index(drop=True)
    footprints["_building_id"] = footprints.index
    pairs = vectorized_overlay_measure(
        footprints,
        geometries_work,
        feature_id_col="_building_id",
        zone_id_col="_geometry_row_id",
        measure="area",
    )
    if pairs.empty:
        return weights_by_row.groupby(geometries_work[id_col]).sum()
    totals = pairs.groupby("_geometry_row_id")["intersection_measure"].sum()
    weights_by_row.loc[totals.index] = totals
    return weights_by_row.groupby(geometries_work[id_col]).sum()


def population_within_polygon(
    population_cells: gpd.GeoDataFrame,
    catchment_polygon,
    *,
    population_col: str = "population",
    method: str = "area_weighted",
    buildings: gpd.GeoDataFrame | None = None,
) -> tuple[float, gpd.GeoDataFrame]:
    """Estimate population covered by a catchment polygon.

    ``area_weighted`` is preferred for clipped/refined cells. ``centroid`` is
    provided only to reproduce older notebook behavior.
    """
    if method not in {"area_weighted", "building_weighted", "auto", "centroid"}:
        raise ValueError("method must be area_weighted, building_weighted, auto, or centroid")
    if population_cells.empty or catchment_polygon is None or catchment_polygon.is_empty:
        return 0.0, population_cells.iloc[0:0].copy()

    catchment = gpd.GeoDataFrame(geometry=[catchment_polygon], crs=population_cells.crs)
    if method == "centroid":
        temp = population_cells.copy()
        temp["_orig_geometry"] = temp.geometry
        temp.geometry = temp.geometry.centroid
        covered = gpd.sjoin(temp, catchment, predicate="within", how="inner").drop(columns=["index_right"])
        covered.geometry = covered["_orig_geometry"]
        covered = covered.drop(columns=["_orig_geometry"])
        return float(covered[population_col].sum()), covered

    cells = population_cells.copy()
    cells["_cell_id"] = range(len(cells))
    cells["_cell_area_sqm"] = cells.geometry.area
    intersections = gpd.overlay(cells, catchment, how="intersection")
    if intersections.empty:
        return 0.0, cells.iloc[0:0].copy()
    intersections["_intersect_area_sqm"] = intersections.geometry.area
    intersections["_area_coverage_fraction"] = (
        intersections["_intersect_area_sqm"] / intersections["_cell_area_sqm"]
    ).clip(0, 1).fillna(0)
    intersections["_coverage_method"] = "area_weighted"

    use_buildings = method in {"building_weighted", "auto"} and buildings is not None and not buildings.empty
    if use_buildings:
        total_building_area = _building_area_in_geometries(buildings, cells, "_cell_id")
        covered_building_area = _building_area_in_geometries(buildings, intersections, "_cell_id")
        intersections["_cell_building_area_sqm"] = intersections["_cell_id"].map(total_building_area).fillna(0)
        intersections["_covered_building_area_sqm"] = intersections["_cell_id"].map(covered_building_area).fillna(0)
        building_fraction = (
            intersections["_covered_building_area_sqm"]
            / intersections["_cell_building_area_sqm"].where(intersections["_cell_building_area_sqm"] != 0)
        ).clip(0, 1)
        intersections["_coverage_fraction"] = building_fraction.fillna(intersections["_area_coverage_fraction"])
        intersections.loc[intersections["_cell_building_area_sqm"] > 0, "_coverage_method"] = "building_weighted"
    else:
        intersections["_coverage_fraction"] = intersections["_area_coverage_fraction"]
    intersections["covered_population"] = intersections[population_col] * intersections["_coverage_fraction"]
    return float(intersections["covered_population"].sum()), intersections


def population_within_catchments(
    population_cells: gpd.GeoDataFrame,
    catchments: gpd.GeoDataFrame,
    *,
    population_col: str = "population",
    dissolve: bool = True,
    method: str = "area_weighted",
    buildings: gpd.GeoDataFrame | None = None,
) -> tuple[float, gpd.GeoDataFrame]:
    """Calculate covered population, dissolving catchments by default to avoid double counting."""
    if catchments.empty:
        return 0.0, population_cells.iloc[0:0].copy()
    catchments_work = catchments.to_crs(population_cells.crs) if catchments.crs != population_cells.crs else catchments
    geom = unary_union(catchments_work.geometry) if dissolve else catchments_work.geometry.unary_union
    return population_within_polygon(
        population_cells,
        geom,
        population_col=population_col,
        method=method,
        buildings=buildings,
    )


def _population_by_catchment(
    population_cells: gpd.GeoDataFrame,
    catchments: gpd.GeoDataFrame,
    *,
    station_id_col: str,
    population_col: str,
    method: str,
    buildings: gpd.GeoDataFrame | None,
) -> pd.Series:
    """Calculate all per-station populations in one spatial-overlay pass."""
    station_ids = pd.Index(catchments[station_id_col].drop_duplicates(), name=station_id_col)
    empty = pd.Series(0.0, index=station_ids, dtype=float)
    if population_cells.empty or catchments.empty:
        return empty

    cells = population_cells.copy().reset_index(drop=True)
    cells["_cell_id"] = range(len(cells))
    cells["_cell_area_sqm"] = cells.geometry.area
    catchments_work = catchments.to_crs(cells.crs) if catchments.crs != cells.crs else catchments
    intersections = gpd.overlay(
        cells[["_cell_id", population_col, "_cell_area_sqm", "geometry"]],
        catchments_work[[station_id_col, "geometry"]],
        how="intersection",
        keep_geom_type=False,
    )
    if intersections.empty:
        return empty
    intersections["_intersect_area_sqm"] = intersections.geometry.area
    intersections = intersections[intersections["_intersect_area_sqm"] > 0].copy().reset_index(drop=True)
    if intersections.empty:
        return empty
    intersections["_intersection_id"] = range(len(intersections))
    area_fraction = (
        intersections["_intersect_area_sqm"]
        / intersections["_cell_area_sqm"].where(intersections["_cell_area_sqm"] > 0)
    ).clip(0, 1).fillna(0.0)
    coverage_fraction = area_fraction

    use_buildings = method in {"building_weighted", "auto"} and buildings is not None and not buildings.empty
    if use_buildings:
        total_building_area = _building_area_in_geometries(buildings, cells, "_cell_id")
        covered_building_area = _building_area_in_geometries(buildings, intersections, "_intersection_id")
        total = intersections["_cell_id"].map(total_building_area).fillna(0.0)
        covered = intersections["_intersection_id"].map(covered_building_area).fillna(0.0)
        building_fraction = (covered / total.where(total > 0)).clip(0, 1)
        coverage_fraction = building_fraction.fillna(area_fraction)

    intersections["_covered_population"] = intersections[population_col] * coverage_fraction
    totals = intersections.groupby(station_id_col)["_covered_population"].sum()
    return totals.reindex(station_ids, fill_value=0.0).astype(float)


def filter_stations_by_catchment_population(
    stations: gpd.GeoDataFrame,
    catchments: gpd.GeoDataFrame,
    urban_population_cells: gpd.GeoDataFrame,
    *,
    all_population_cells: gpd.GeoDataFrame | None = None,
    station_id_col: str = "station_id",
    min_population: float = 500.0,
    min_urban_share: float = 0.25,
    population_col: str = "population",
    method: str = "area_weighted",
    buildings: gpd.GeoDataFrame | None = None,
) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """Filter stations using walking-catchment population rather than station point location."""
    if stations.empty or catchments.empty:
        diagnostics = gpd.GeoDataFrame(columns=[station_id_col, "urban_catchment_population", "urban_share"])
        return stations.iloc[0:0].copy(), diagnostics

    stations_work = stations.copy()
    catchments_work = catchments.to_crs(urban_population_cells.crs) if catchments.crs != urban_population_cells.crs else catchments.copy()
    if station_id_col not in stations_work.columns:
        stations_work[station_id_col] = range(len(stations_work))
    if station_id_col not in catchments_work.columns:
        if len(catchments_work) == len(stations_work):
            catchments_work[station_id_col] = list(stations_work[station_id_col])
        else:
            catchments_work[station_id_col] = range(len(catchments_work))

    urban_populations = _population_by_catchment(
        urban_population_cells,
        catchments_work,
        station_id_col=station_id_col,
        population_col=population_col,
        method=method,
        buildings=buildings,
    )
    diagnostics = catchments_work[[station_id_col, "geometry"]].copy()
    diagnostics["urban_catchment_population"] = diagnostics[station_id_col].map(urban_populations).fillna(0.0)
    if all_population_cells is not None:
        total_populations = _population_by_catchment(
            all_population_cells,
            catchments_work,
            station_id_col=station_id_col,
            population_col=population_col,
            method=method,
            buildings=buildings,
        )
        diagnostics["total_catchment_population"] = diagnostics[station_id_col].map(total_populations).fillna(0.0)
        diagnostics["urban_share"] = (
            diagnostics["urban_catchment_population"]
            / diagnostics["total_catchment_population"].where(diagnostics["total_catchment_population"] > 0)
        ).fillna(0.0)
        share_ok = diagnostics["urban_share"] >= min_urban_share
    else:
        diagnostics["total_catchment_population"] = None
        diagnostics["urban_share"] = None
        share_ok = pd.Series(False, index=diagnostics.index)
    diagnostics["keep_station"] = (
        diagnostics["urban_catchment_population"] >= min_population
    ) | share_ok
    kept_ids = set(diagnostics.loc[diagnostics["keep_station"], station_id_col])
    return stations_work[stations_work[station_id_col].isin(kept_ids)].copy(), diagnostics
