"""Geometry utilities used by the urban-area pipeline."""

from __future__ import annotations

from collections import defaultdict

import geopandas as gpd
import pandas as pd
import shapely
from shapely.geometry import GeometryCollection, MultiPolygon, Polygon
from shapely.ops import unary_union
from shapely.wkb import dumps as wkb_dumps, loads as wkb_loads


def force_2d(geom):
    """Drop Z coordinates if present."""
    if geom is None or geom.is_empty:
        return geom
    try:
        return wkb_loads(wkb_dumps(geom, output_dimension=2))
    except Exception:
        return geom


def make_valid_2d(geom):
    if geom is None or geom.is_empty:
        return geom
    geom = force_2d(geom)
    if geom.is_valid:
        return geom
    try:
        return force_2d(geom.buffer(0))
    except Exception:
        return geom


def polygon_parts(geom) -> list[Polygon]:
    """Extract polygon parts from polygons, multipolygons, and collections."""
    if geom is None or geom.is_empty:
        return []
    if isinstance(geom, Polygon):
        return [geom]
    if isinstance(geom, MultiPolygon):
        return [p for p in geom.geoms if not p.is_empty]
    if isinstance(geom, GeometryCollection):
        result: list[Polygon] = []
        for part in geom.geoms:
            result.extend(polygon_parts(part))
        return result
    return []


def clean_union(gdf: gpd.GeoDataFrame):
    """Return a valid Polygon/MultiPolygon union, or None if no polygons exist."""
    if gdf.empty:
        return None
    cleaned = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty].copy()
    cleaned.geometry = cleaned.geometry.apply(make_valid_2d)
    parts = []
    for geom in cleaned.geometry:
        parts.extend(polygon_parts(geom))
    return unary_union(parts) if parts else None


def vectorized_overlay_measure(
    features: gpd.GeoDataFrame,
    zones: gpd.GeoDataFrame,
    *,
    feature_id_col: str,
    zone_id_col: str,
    measure: str,
) -> pd.DataFrame:
    """Measure feature/zone intersections without row-wise geometry loops.

    The result contains one row per candidate pair with the original feature
    and zone identifiers plus ``intersection_measure``. ``measure`` may be
    ``"area"`` or ``"length"``. Both inputs must already use the same
    projected CRS.
    """
    if measure not in {"area", "length"}:
        raise ValueError("measure must be 'area' or 'length'")
    if feature_id_col not in features or zone_id_col not in zones:
        raise ValueError("Feature and zone identifier columns are required")
    if features.crs != zones.crs:
        raise ValueError("Features and zones must use the same CRS")

    columns = [feature_id_col, zone_id_col, "intersection_measure"]
    if features.empty or zones.empty:
        return pd.DataFrame(columns=columns)

    feature_work = features[[feature_id_col, "geometry"]].copy().reset_index(drop=True)
    feature_work["__feature_pos"] = range(len(feature_work))
    zone_work = zones[[zone_id_col, "geometry"]].copy().reset_index(drop=True)
    zone_work["__zone_pos"] = range(len(zone_work))
    pairs = gpd.sjoin(
        feature_work[["__feature_pos", "geometry"]],
        zone_work[["__zone_pos", "geometry"]],
        how="inner",
        predicate="intersects",
    )
    if pairs.empty:
        return pd.DataFrame(columns=columns)

    feature_pos = pairs["__feature_pos"].to_numpy(dtype=int)
    zone_pos = pairs["__zone_pos"].to_numpy(dtype=int)
    intersections = shapely.intersection(
        feature_work.geometry.to_numpy()[feature_pos],
        zone_work.geometry.to_numpy()[zone_pos],
    )
    values = shapely.area(intersections) if measure == "area" else shapely.length(intersections)
    result = pd.DataFrame(
        {
            feature_id_col: feature_work[feature_id_col].to_numpy()[feature_pos],
            zone_id_col: zone_work[zone_id_col].to_numpy()[zone_pos],
            "intersection_measure": values,
        }
    )
    return result[result["intersection_measure"].notna() & (result["intersection_measure"] >= 0)].reset_index(
        drop=True
    )


def build_adjacency_graph(
    gdf: gpd.GeoDataFrame,
    id_col: str = "cell_id",
    *,
    mode: str = "edge",
    min_shared_boundary_m: float = 1.0,
) -> dict[int, set[int]]:
    """Build a cell adjacency graph.

    ``mode='edge'`` requires shared boundary length and avoids point-only
    contacts. ``mode='touches'`` preserves the prototype behavior.
    """
    if mode not in {"edge", "touches", "intersects"}:
        raise ValueError("mode must be one of: edge, touches, intersects")
    cells = gdf[[id_col, "geometry"]].copy()
    cells = cells[cells.geometry.notna() & ~cells.geometry.is_empty].reset_index(drop=True)
    cells["__left_pos"] = range(len(cells))
    right = cells[[id_col, "geometry"]].rename(columns={id_col: "__right_id"}).copy()
    right["__right_pos"] = range(len(right))
    adjacency: dict[int, set[int]] = defaultdict(set)
    if cells.empty:
        return {}

    pairs = gpd.sjoin(
        cells[["__left_pos", "geometry"]],
        right[["__right_pos", "__right_id", "geometry"]],
        how="inner",
        predicate="intersects",
    )
    pairs = pairs[pairs["__left_pos"] < pairs["__right_pos"]].copy()
    if pairs.empty:
        return {}

    left_pos = pairs["__left_pos"].to_numpy(dtype=int)
    right_pos = pairs["__right_pos"].to_numpy(dtype=int)
    left_geoms = cells.geometry.to_numpy()[left_pos]
    right_geoms = cells.geometry.to_numpy()[right_pos]
    if mode == "touches":
        neighbor_mask = shapely.touches(left_geoms, right_geoms)
    elif mode == "intersects":
        neighbor_mask = shapely.intersects(left_geoms, right_geoms)
    else:
        shared = shapely.intersection(shapely.boundary(left_geoms), shapely.boundary(right_geoms))
        neighbor_mask = shapely.length(shared) >= min_shared_boundary_m

    ids = cells[id_col].to_numpy()
    for left_idx, right_idx in zip(left_pos[neighbor_mask], right_pos[neighbor_mask]):
        left_id = ids[left_idx]
        right_id = ids[right_idx]
        adjacency[left_id].add(right_id)
        adjacency[right_id].add(left_id)
    return dict(adjacency)


def component_labels(gdf: gpd.GeoDataFrame, adjacency: dict[int, set[int]], id_col: str = "cell_id") -> list[int]:
    """Return connected-component labels using the supplied adjacency graph."""
    ids = list(gdf[id_col])
    id_set = set(ids)
    labels = {}
    current = 0
    for start in ids:
        if start in labels:
            continue
        stack = [start]
        labels[start] = current
        while stack:
            cid = stack.pop()
            for neighbor in adjacency.get(cid, set()):
                if neighbor in id_set and neighbor not in labels:
                    labels[neighbor] = current
                    stack.append(neighbor)
        current += 1
    return [labels[cid] for cid in ids]


def remove_interior_holes(geom):
    """Drop polygon interiors from a Polygon/MultiPolygon."""
    if geom is None or geom.is_empty:
        return geom
    if isinstance(geom, Polygon):
        return Polygon(geom.exterior)
    if isinstance(geom, MultiPolygon):
        return MultiPolygon([Polygon(poly.exterior) for poly in geom.geoms if not poly.is_empty])
    return geom
