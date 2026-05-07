"""Geometry utilities used by the urban-area pipeline."""

from __future__ import annotations

from collections import defaultdict

import geopandas as gpd
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
    cells = gdf[[id_col, "geometry"]].copy().reset_index(drop=True)
    sindex = cells.sindex
    adjacency: dict[int, set[int]] = defaultdict(set)

    for idx, row in cells.iterrows():
        cid = row[id_col]
        geom = row.geometry
        if geom is None or geom.is_empty:
            continue
        for cand_idx in sindex.intersection(geom.bounds):
            if cand_idx <= idx:
                continue
            cand = cells.iloc[cand_idx]
            other = cand.geometry
            if other is None or other.is_empty:
                continue
            is_neighbor = False
            if mode == "touches":
                is_neighbor = geom.touches(other)
            elif mode == "intersects":
                is_neighbor = geom.intersects(other)
            else:
                boundary = geom.boundary.intersection(other.boundary)
                is_neighbor = getattr(boundary, "length", 0.0) >= min_shared_boundary_m
            if is_neighbor:
                adjacency[cid].add(cand[id_col])
                adjacency[cand[id_col]].add(cid)
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
