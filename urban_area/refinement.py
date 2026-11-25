"""Boundary refinement for urban cells."""

from __future__ import annotations

import warnings

import geopandas as gpd
import pandas as pd
from shapely.geometry import Polygon

from .buildings import aggregate_buildings_to_cells
from .geometry import build_adjacency_graph, component_labels, vectorized_overlay_measure
from .growth import _components_to_keep
from .metrics import NORMALISED_SCORE_COLUMNS, add_absolute_metrics
from .roads import aggregate_roads_to_cells
from .schemas import POINT_TYPES, POLYGON_TYPES


RESIDENTIAL_BUILDING_VALUES = {
    "apartments",
    "bungalow",
    "cabin",
    "detached",
    "dormitory",
    "farm",
    "house",
    "residential",
    "semidetached_house",
    "static_caravan",
    "terrace",
}


def _subdivide_polygon_rows(edge_cells: gpd.GeoDataFrame, subdivision_factor: int) -> gpd.GeoDataFrame:
    subs = []
    parent_ids = []
    parent_names = []
    for _, row in edge_cells.iterrows():
        geom = row.geometry
        minx, miny, maxx, maxy = geom.bounds
        dx = (maxx - minx) / subdivision_factor
        dy = (maxy - miny) / subdivision_factor
        for i in range(subdivision_factor):
            for j in range(subdivision_factor):
                rect = Polygon(
                    [
                        (minx + i * dx, miny + j * dy),
                        (minx + (i + 1) * dx, miny + j * dy),
                        (minx + (i + 1) * dx, miny + (j + 1) * dy),
                        (minx + i * dx, miny + (j + 1) * dy),
                    ]
                )
                part = rect if geom.contains(rect) else rect.intersection(geom)
                if not part.is_empty and getattr(part, "area", 0.0) > 0:
                    subs.append(part)
                    parent_ids.append(row["cell_id"])
                    parent_names.append(row.get("Name"))
    return gpd.GeoDataFrame(
        {
            "parent_cell_id": parent_ids,
            "Name": parent_names,
            "geometry": subs,
        },
        crs=edge_cells.crs,
    )


def _is_residential_building_row(row) -> bool:
    for col in ["building", "building:use", "building_use", "use", "landuse"]:
        if col not in row.index:
            continue
        value = row[col]
        if isinstance(value, (list, tuple, set)):
            values = {str(v).lower() for v in value if v is not None}
        elif pd.isna(value):
            values = set()
        else:
            values = {str(value).lower()}
        if values & RESIDENTIAL_BUILDING_VALUES:
            return True
    return False


def _polygon_area_weights(features: gpd.GeoDataFrame, subcells: gpd.GeoDataFrame) -> pd.Series:
    weights = pd.Series(0.0, index=subcells["cell_id"])
    polygons = features[features.geometry.notna() & features.geometry.is_valid & features.geom_type.isin(POLYGON_TYPES)].copy()
    if polygons.empty or subcells.empty:
        return weights
    polygons = polygons.reset_index(drop=True)
    polygons["_feature_id"] = polygons.index
    pairs = vectorized_overlay_measure(
        polygons,
        subcells,
        feature_id_col="_feature_id",
        zone_id_col="cell_id",
        measure="area",
    )
    if pairs.empty:
        return weights
    totals = pairs.groupby("cell_id")["intersection_measure"].sum()
    weights.loc[totals.index] = totals
    return weights


def _point_count_weights(features: gpd.GeoDataFrame, subcells: gpd.GeoDataFrame) -> pd.Series:
    weights = pd.Series(0.0, index=subcells["cell_id"])
    points = features[features.geometry.notna() & features.geometry.is_valid & features.geom_type.isin(POINT_TYPES)].copy()
    if points.empty or subcells.empty:
        return weights
    points = points.reset_index(drop=True)
    points["_feature_id"] = points.index
    joined = gpd.sjoin(
        points[["_feature_id", "geometry"]],
        subcells[["cell_id", "geometry"]],
        how="inner",
        predicate="within",
    )
    if joined.empty:
        return weights
    counts = joined.groupby("cell_id")["_feature_id"].nunique()
    weights.loc[counts.index] = counts.astype(float)
    return weights


def allocate_population_dasymetric(
    subcells: gpd.GeoDataFrame,
    parent_cells: gpd.GeoDataFrame,
    buildings: gpd.GeoDataFrame,
) -> gpd.GeoDataFrame:
    """Allocate parent-cell population to refined subcells using built-form evidence."""
    result = subcells.copy()
    parent_lookup = parent_cells.set_index("cell_id")
    result["parent_population"] = result["parent_cell_id"].map(parent_lookup["population"])
    result["parent_area_sqm"] = result["parent_cell_id"].map(parent_lookup.geometry.area)
    result["cell_area_sqm"] = result.geometry.area
    result["population"] = 0.0
    result["population_allocation_weight"] = 0.0
    result["population_allocation_method"] = "area_weighted_fallback"

    buildings_work = buildings.to_crs(result.crs) if not buildings.empty and buildings.crs != result.crs else buildings.copy()
    residential = buildings_work.iloc[0:0].copy()
    if not buildings_work.empty and any(t in set(buildings_work.geom_type.unique()) for t in POLYGON_TYPES):
        residential_mask = buildings_work.apply(_is_residential_building_row, axis=1)
        residential = buildings_work[residential_mask].copy()

    residential_weights = _polygon_area_weights(residential, result)
    footprint_weights = _polygon_area_weights(buildings_work, result)
    point_weights = _point_count_weights(buildings_work, result)
    area_weights = result.set_index("cell_id").geometry.area

    for parent_id, group in result.groupby("parent_cell_id"):
        idx = group["cell_id"]
        rows = group.index
        parent_population = float(parent_lookup.loc[parent_id, "population"]) if parent_id in parent_lookup.index else 0.0
        candidate_methods = [
            ("residential_building_footprints", residential_weights.loc[idx]),
            ("building_footprints", footprint_weights.loc[idx]),
            ("building_points", point_weights.loc[idx]),
            ("area_weighted_fallback", area_weights.loc[idx]),
        ]
        method = "area_weighted_fallback"
        weights = area_weights.loc[idx]
        for candidate_method, candidate_weights in candidate_methods:
            if float(candidate_weights.sum()) > 0:
                method = candidate_method
                weights = candidate_weights
                break
        total_weight = float(weights.sum())
        fractions = weights / total_weight if total_weight > 0 else weights * 0
        result.loc[rows, "population"] = parent_population * fractions.to_numpy()
        result.loc[rows, "population_allocation_weight"] = weights.to_numpy()
        result.loc[rows, "population_allocation_method"] = method

    return result


def refine_urban_edges(
    urban_cells: gpd.GeoDataFrame,
    all_analysis_cells: gpd.GeoDataFrame,
    buildings: gpd.GeoDataFrame,
    roads: gpd.GeoDataFrame,
    *,
    building_mode: str,
    subdivision_factor: int = 4,
    min_building_area_sqm: float = 600.0,
    min_road_density_km_per_sqkm: float = 3.0,
    require_local_presence: bool = True,
    adjacency_mode: str = "edge",
    adjacency: dict[int, set[int]] | None = None,
    local_road_values: list[str] | None = None,
    major_road_values: list[str] | None = None,
    keep_largest_component: bool = True,
    min_component_population: float = 0.0,
    min_component_area_sqkm: float = 0.0,
    min_component_built_up_area_sqm: float = 0.0,
) -> gpd.GeoDataFrame:
    """Refine edge cells into smaller polygons while preserving schema."""
    if urban_cells.empty:
        return urban_cells.copy()
    urban = urban_cells.copy()
    analysis = all_analysis_cells.copy()
    if "cell_id" not in urban.columns:
        urban["cell_id"] = range(len(urban))
    if "cell_id" not in analysis.columns:
        analysis["cell_id"] = range(len(analysis))

    if adjacency is None:
        adjacency = build_adjacency_graph(analysis, id_col="cell_id", mode=adjacency_mode)
    urban_ids = set(urban["cell_id"])
    edge_ids = [
        cid for cid in urban_ids
        if any(neighbor not in urban_ids for neighbor in adjacency.get(cid, set()))
    ]
    if not edge_ids:
        result = urban.copy()
        result["is_refined"] = False
        result["parent_cell_id"] = result["cell_id"]
        return result

    edge_cells = urban[urban["cell_id"].isin(edge_ids)].copy()
    subcells = _subdivide_polygon_rows(edge_cells, subdivision_factor)
    if subcells.empty:
        return urban
    subcells["cell_id"] = range(len(subcells))

    parent_lookup = urban.set_index("cell_id")
    subcells = allocate_population_dasymetric(subcells, urban, buildings)
    subcells["cell_area_sqkm"] = subcells["cell_area_sqm"] / 1_000_000

    building_result = aggregate_buildings_to_cells(buildings, subcells, building_mode=building_mode)
    subcells = building_result.cells
    subcells = aggregate_roads_to_cells(
        roads,
        subcells,
        local_values=local_road_values,
        major_values=major_road_values,
    )

    has_building_presence = (subcells["total_building_area_sqm"] >= 100.0) | (subcells["building_point_count"] >= 1)
    population_presence = subcells["population"] >= (0.01 * subcells["parent_population"])
    presence_ok = (~require_local_presence) | has_building_presence | population_presence

    if building_mode == "point":
        building_ok = subcells["building_point_count"] >= 1
    else:
        building_ok = subcells["building_area_sqm"] >= min_building_area_sqm
    road_density_col = (
        "local_road_density_km_per_sqkm"
        if "local_road_density_km_per_sqkm" in subcells.columns
        else "road_density_km_per_sqkm"
    )
    road_ok = subcells[road_density_col] >= min_road_density_km_per_sqkm
    urban_sub = subcells[presence_ok & (building_ok | road_ok)].copy()

    non_edge = urban[~urban["cell_id"].isin(edge_ids)].copy()
    non_edge["is_refined"] = False
    non_edge["parent_cell_id"] = non_edge["cell_id"]
    non_edge["population_allocation_method"] = "unrefined_parent_cell"
    non_edge["population_allocation_weight"] = pd.NA
    urban_sub["is_refined"] = True
    urban_sub["h3"] = pd.NA

    if "Name" in urban.columns and "Name" not in urban_sub.columns:
        urban_sub["Name"] = urban_sub["parent_cell_id"].map(parent_lookup["Name"])

    base_cols = [c for c in non_edge.columns if c != "geometry"]
    extra_cols = [c for c in urban_sub.columns if c not in base_cols and c != "geometry"]
    output_cols = base_cols + extra_cols
    for col in output_cols:
        if col not in urban_sub.columns:
            urban_sub[col] = pd.NA
        if col not in non_edge.columns:
            non_edge[col] = pd.NA
    left = non_edge[output_cols + ["geometry"]].copy()
    right = urban_sub[output_cols + ["geometry"]].copy()
    for col in output_cols:
        try:
            if not left.empty and left[col].isna().all() and not right[col].isna().all():
                left[col] = left[col].astype(right[col].dtype)
            elif not right.empty and right[col].isna().all() and not left[col].isna().all():
                right[col] = right[col].astype(left[col].dtype)
        except (TypeError, ValueError):
            pass
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="The behavior of DataFrame concatenation with empty or all-NA entries is deprecated.*",
            category=FutureWarning,
        )
        refined = pd.concat([left, right], ignore_index=True)
    refined = gpd.GeoDataFrame(refined, geometry="geometry", crs=urban.crs)
    refined = refined.reset_index(drop=True)
    refined["cell_id"] = range(len(refined))

    if len(refined) > 1:
        refined_adj = build_adjacency_graph(refined, id_col="cell_id", mode=adjacency_mode)
        refined["component"] = component_labels(refined, refined_adj, id_col="cell_id")
        component_stats = refined.groupby("component").agg(
            population=("population", "sum"),
            built_up_area_sqm=("building_area_sqm", "sum"),
        )
        component_stats["area_sqkm"] = refined.groupby("component").geometry.apply(lambda s: s.area.sum() / 1_000_000)
        keep_components = _components_to_keep(
            component_stats,
            keep_largest_component=keep_largest_component,
            min_population=min_component_population,
            min_area_sqkm=min_component_area_sqkm,
            min_built_up_area_sqm=min_component_built_up_area_sqm,
        )
        refined = refined[refined["component"].isin(keep_components)].copy()

    refined = refined.drop(columns=["component"], errors="ignore").reset_index(drop=True)
    refined["cell_id"] = range(len(refined))
    # Percentile-normalised scores are only meaningful across the whole
    # analysis envelope, so they are not recomputed on the urban subset.
    refined = refined.drop(columns=NORMALISED_SCORE_COLUMNS, errors="ignore")
    return add_absolute_metrics(refined, building_mode=building_mode)
