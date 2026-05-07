"""Road-network aggregation for urban-area detection."""

from __future__ import annotations

import geopandas as gpd
import pandas as pd

from .config import LOCAL_URBAN_FABRIC_ROAD_VALUES, MAJOR_TRANSPORT_ROAD_VALUES
from .schemas import LINE_TYPES


def _first_highway_value(value) -> str | None:
    if isinstance(value, (list, tuple, set)):
        for item in value:
            if item is not None:
                return str(item)
        return None
    if value is None or pd.isna(value):
        return None
    return str(value)


def classify_road_role(
    highway_value,
    *,
    local_values: list[str] | None = None,
    major_values: list[str] | None = None,
) -> str:
    """Classify roads for morphology scoring versus diagnostics."""
    local = set(local_values or LOCAL_URBAN_FABRIC_ROAD_VALUES)
    major = set(major_values or MAJOR_TRANSPORT_ROAD_VALUES)
    value = _first_highway_value(highway_value)
    if value in major:
        return "major_transport"
    if value in local or value is None:
        return "local_urban_fabric"
    return "other"


def normalize_roads(
    roads: gpd.GeoDataFrame,
    target_crs: str | object,
    *,
    deduplicate: bool = True,
    local_values: list[str] | None = None,
    major_values: list[str] | None = None,
) -> gpd.GeoDataFrame:
    """Filter roads to line geometries, project them, and calculate length."""
    if roads.crs != target_crs:
        roads = roads.to_crs(target_crs)
    result = roads[roads.geometry.notna() & roads.geometry.is_valid & roads.geom_type.isin(LINE_TYPES)].copy()
    if result.empty:
        result["length_m"] = []
        result["road_role"] = []
        return result
    if deduplicate:
        id_col = "osmid" if "osmid" in result.columns else "id" if "id" in result.columns else None
        if id_col:
            result["_dedupe_key"] = result[id_col].astype(str)
            result = result.drop_duplicates("_dedupe_key").drop(columns=["_dedupe_key"])
        else:
            result["_dedupe_key"] = result.geometry.apply(lambda g: g.wkb_hex)
            result = result.drop_duplicates("_dedupe_key").drop(columns=["_dedupe_key"])
    result["length_m"] = result.geometry.length
    if "highway" in result.columns:
        result["road_role"] = result["highway"].apply(
            classify_road_role,
            local_values=local_values,
            major_values=major_values,
        )
    else:
        result["road_role"] = "local_urban_fabric"
    return result


def aggregate_roads_to_cells(
    roads: gpd.GeoDataFrame,
    cells: gpd.GeoDataFrame,
    *,
    cell_id_col: str = "cell_id",
    local_values: list[str] | None = None,
    major_values: list[str] | None = None,
) -> gpd.GeoDataFrame:
    """Aggregate intersected road lengths to projected analysis cells."""
    if cells.crs is None:
        raise ValueError("Cells must have a metric CRS before road aggregation")
    if cell_id_col not in cells.columns:
        raise ValueError(f"Cells are missing required id column {cell_id_col!r}")

    result = cells.copy()
    result["cell_area_sqm"] = result.geometry.area
    result["total_road_length_m"] = 0.0
    result["local_road_length_m"] = 0.0
    result["major_road_length_m"] = 0.0
    result["road_segment_count"] = 0
    result["local_road_segment_count"] = 0
    result["major_road_segment_count"] = 0
    if roads.empty:
        result["road_density_km_per_sqkm"] = 0.0
        result["local_road_density_km_per_sqkm"] = 0.0
        result["major_road_density_km_per_sqkm"] = 0.0
        return result

    roads_work = normalize_roads(
        roads,
        result.crs,
        local_values=local_values,
        major_values=major_values,
    )
    roads_work = roads_work.reset_index(drop=True)
    roads_work["road_id"] = roads_work.index
    if roads_work.empty:
        result["road_density_km_per_sqkm"] = 0.0
        result["local_road_density_km_per_sqkm"] = 0.0
        result["major_road_density_km_per_sqkm"] = 0.0
        return result

    joined = gpd.sjoin(
        roads_work[["road_id", "geometry"]],
        result[[cell_id_col, "geometry"]],
        how="inner",
        predicate="intersects",
    )
    if not joined.empty:
        stats = []
        cell_geom = result.set_index(cell_id_col).geometry
        for cell_id, group in joined.groupby(cell_id_col):
            geom = cell_geom.loc[cell_id]
            total_length = 0.0
            local_length = 0.0
            major_length = 0.0
            ids = set()
            local_ids = set()
            major_ids = set()
            for _, row in group.iterrows():
                rgeom = roads_work.loc[row["road_id"], "geometry"]
                try:
                    clipped = rgeom.intersection(geom)
                    length = clipped.length
                    total_length += length
                    ids.add(row["road_id"])
                    role = roads_work.loc[row["road_id"], "road_role"]
                    if role == "major_transport":
                        major_length += length
                        major_ids.add(row["road_id"])
                    elif role == "local_urban_fabric":
                        local_length += length
                        local_ids.add(row["road_id"])
                except Exception:
                    continue
            stats.append(
                {
                    cell_id_col: cell_id,
                    "total_road_length_m": total_length,
                    "local_road_length_m": local_length,
                    "major_road_length_m": major_length,
                    "road_segment_count": len(ids),
                    "local_road_segment_count": len(local_ids),
                    "major_road_segment_count": len(major_ids),
                }
            )
        stats_df = pd.DataFrame(stats)
        result = result.drop(
            columns=[
                "total_road_length_m",
                "local_road_length_m",
                "major_road_length_m",
                "road_segment_count",
                "local_road_segment_count",
                "major_road_segment_count",
            ]
        ).merge(
            stats_df, on=cell_id_col, how="left"
        )
        result["total_road_length_m"] = result["total_road_length_m"].fillna(0.0)
        result["local_road_length_m"] = result["local_road_length_m"].fillna(0.0)
        result["major_road_length_m"] = result["major_road_length_m"].fillna(0.0)
        result["road_segment_count"] = result["road_segment_count"].fillna(0).astype(int)
        result["local_road_segment_count"] = result["local_road_segment_count"].fillna(0).astype(int)
        result["major_road_segment_count"] = result["major_road_segment_count"].fillna(0).astype(int)

    area_sqkm = result["cell_area_sqm"] / 1_000_000
    safe_area_sqkm = area_sqkm.where(area_sqkm != 0)
    result["road_density_km_per_sqkm"] = (
        ((result["total_road_length_m"] / 1000) / safe_area_sqkm)
        .replace([float("inf"), -float("inf")], 0)
        .fillna(0)
    )
    result["local_road_density_km_per_sqkm"] = (
        ((result["local_road_length_m"] / 1000) / safe_area_sqkm)
        .replace([float("inf"), -float("inf")], 0)
        .fillna(0)
    )
    result["major_road_density_km_per_sqkm"] = (
        ((result["major_road_length_m"] / 1000) / safe_area_sqkm)
        .replace([float("inf"), -float("inf")], 0)
        .fillna(0)
    )
    return result
