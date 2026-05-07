"""Building-footprint and building-point aggregation."""

from __future__ import annotations

from dataclasses import dataclass

import geopandas as gpd
import pandas as pd

from .schemas import POINT_TYPES, POLYGON_TYPES


@dataclass(frozen=True)
class BuildingAggregationResult:
    cells: gpd.GeoDataFrame
    mode: str
    source_geometry_types: dict[str, int]
    warnings: list[str]


def building_geometry_counts(buildings: gpd.GeoDataFrame) -> dict[str, int]:
    if buildings.empty:
        return {}
    return {str(k): int(v) for k, v in buildings.geom_type.value_counts(dropna=False).to_dict().items()}


def resolve_building_mode(buildings: gpd.GeoDataFrame, configured_mode: str = "auto") -> str:
    """Resolve the building aggregation mode.

    ``footprint`` uses Polygon/MultiPolygon area coverage. ``point`` uses point
    count/density as the building signal. ``required_footprint`` fails when
    footprints are absent.
    """
    configured_mode = configured_mode.lower()
    if configured_mode not in {"auto", "footprint", "point", "required_footprint"}:
        raise ValueError("building_mode must be auto, footprint, point, or required_footprint")

    types = set(buildings.geom_type.unique()) if not buildings.empty else set()
    has_footprints = bool(types & POLYGON_TYPES)
    has_points = bool(types & POINT_TYPES)

    if configured_mode == "auto":
        if has_footprints:
            return "footprint"
        if has_points:
            return "point"
        raise ValueError("Buildings contain no usable footprint or point geometries")
    if configured_mode in {"footprint", "required_footprint"}:
        if not has_footprints:
            raise ValueError("Building footprints are required but no Polygon/MultiPolygon geometries are present")
        return "footprint"
    if not has_points:
        raise ValueError("Point building mode requested but no Point/MultiPoint geometries are present")
    return "point"


def _empty_building_columns(cells: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    result = cells.copy()
    result["building_area_sqm"] = 0.0
    result["total_building_area_sqm"] = 0.0
    result["building_count"] = 0
    result["building_point_count"] = 0
    result["building_point_density_per_sqkm"] = 0.0
    result["built_up_coverage"] = 0.0
    result["building_coverage"] = 0.0
    result["building_signal_type"] = "none"
    result["building_signal_confidence"] = 0.0
    return result


def aggregate_buildings_to_cells(
    buildings: gpd.GeoDataFrame,
    cells: gpd.GeoDataFrame,
    *,
    building_mode: str = "auto",
    cell_id_col: str = "cell_id",
) -> BuildingAggregationResult:
    """Aggregate buildings to projected analysis cells.

    The caller is expected to pass cells in the city metric CRS. Buildings are
    reprojected to that CRS before areas/counts are calculated.
    """
    if cells.crs is None:
        raise ValueError("Cells must have a metric CRS before building aggregation")
    if cell_id_col not in cells.columns:
        raise ValueError(f"Cells are missing required id column {cell_id_col!r}")

    source_counts = building_geometry_counts(buildings)
    mode = resolve_building_mode(buildings, building_mode)
    warnings: list[str] = []
    if mode == "point":
        warnings.append(
            "Only point building geometries are available; building signal uses lower-confidence point density fallback"
        )
    cells_work = cells.copy()
    cells_work["cell_area_sqm"] = cells_work.geometry.area
    buildings_work = buildings.to_crs(cells_work.crs) if buildings.crs != cells_work.crs else buildings.copy()
    buildings_work = buildings_work[buildings_work.geometry.notna() & buildings_work.geometry.is_valid].copy()

    result = _empty_building_columns(cells_work)
    if buildings_work.empty:
        warnings.append("Building source was present but no valid building geometries remained after CRS/type filtering")
        return BuildingAggregationResult(result, mode, source_counts, warnings)

    if mode == "footprint":
        footprints = buildings_work[buildings_work.geom_type.isin(POLYGON_TYPES)].copy()
        if footprints.empty:
            raise ValueError("Building mode resolved to footprint, but no valid footprints remain after filtering")
        footprints = footprints.reset_index(drop=True)
        footprints["building_id"] = footprints.index
        joined = gpd.sjoin(
            footprints[["building_id", "geometry"]],
            cells_work[[cell_id_col, "geometry"]],
            how="inner",
            predicate="intersects",
        )
        if not joined.empty:
            stats = []
            cell_geom = cells_work.set_index(cell_id_col).geometry
            for cell_id, group in joined.groupby(cell_id_col):
                geom = cell_geom.loc[cell_id]
                total_area = 0.0
                ids = set()
                for _, row in group.iterrows():
                    bgeom = footprints.loc[row["building_id"], "geometry"]
                    try:
                        total_area += bgeom.intersection(geom).area
                        ids.add(row["building_id"])
                    except Exception:
                        continue
                stats.append(
                    {
                        cell_id_col: cell_id,
                        "total_building_area_sqm": total_area,
                        "building_count": len(ids),
                    }
                )
            stats_df = pd.DataFrame(stats)
            result = result.drop(columns=["total_building_area_sqm", "building_count"]).merge(
                stats_df, on=cell_id_col, how="left"
            )
            result["total_building_area_sqm"] = result["total_building_area_sqm"].fillna(0.0)
            result["building_area_sqm"] = result["total_building_area_sqm"]
            result["building_count"] = result["building_count"].fillna(0).astype(int)
        if result["total_building_area_sqm"].sum() == 0:
            warnings.append(
                "Footprint mode found polygon buildings, but aggregated footprint area is zero inside analysis cells"
            )
    else:
        points = buildings_work[buildings_work.geom_type.isin(POINT_TYPES)].copy()
        if points.empty:
            raise ValueError("Building mode resolved to point, but no valid points remain after filtering")
        points = points.reset_index(drop=True)
        points["building_id"] = points.index
        joined = gpd.sjoin(
            points[["building_id", "geometry"]],
            cells_work[[cell_id_col, "geometry"]],
            how="inner",
            predicate="within",
        )
        if not joined.empty:
            stats_df = (
                joined.groupby(cell_id_col)
                .agg(building_point_count=("building_id", "nunique"))
                .reset_index()
            )
            result = result.drop(columns=["building_point_count"]).merge(stats_df, on=cell_id_col, how="left")
            result["building_point_count"] = result["building_point_count"].fillna(0).astype(int)
            result["building_count"] = result["building_point_count"]

    area_sqkm = result["cell_area_sqm"] / 1_000_000
    result["building_area_sqm"] = result["total_building_area_sqm"]
    result["building_coverage"] = (result["total_building_area_sqm"] / result["cell_area_sqm"]).clip(0, 1).fillna(0)
    result["built_up_coverage"] = result["building_coverage"]
    result["building_point_density_per_sqkm"] = (result["building_point_count"] / area_sqkm).replace(
        [float("inf"), -float("inf")], 0
    ).fillna(0)
    if mode == "point":
        result["building_signal_type"] = "point_fallback"
        result["building_signal_confidence"] = 0.5
    else:
        result["building_signal_type"] = "footprint_area"
        result["building_signal_confidence"] = 1.0
    result["building_mode"] = mode
    return BuildingAggregationResult(result, mode, source_counts, warnings)
