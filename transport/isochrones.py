"""Walking-network isochrone construction in a projected CRS."""

from __future__ import annotations

from itertools import chain
from typing import Callable

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely
from shapely.geometry import LineString
from shapely.ops import substring, unary_union

from .network import CsrGraph, nearest_nodes


def reachable_edge_geometries(graph, distances: dict, *, cutoff_m: float) -> list:
    """Return graph edge geometries touched within a distance cutoff."""
    geometries = []
    seen = set()
    reachable_nodes = distances.keys()
    if graph.is_directed():
        # Preserve the prior "either endpoint is reachable" behaviour without
        # scanning every edge in the city graph once per station.
        edges = chain(
            graph.out_edges(reachable_nodes, data=True),
            graph.in_edges(reachable_nodes, data=True),
        )
    else:
        edges = graph.edges(reachable_nodes, data=True)
    for u, v, data in edges:
        distance_u = distances.get(u, float("inf"))
        distance_v = distances.get(v, float("inf"))
        nearest_distance = min(distance_u, distance_v)
        if nearest_distance > cutoff_m:
            continue
        geom = data.get("geometry")
        if geom is None:
            start = graph.nodes[u]
            end = graph.nodes[v]
            geom = LineString([(start["x"], start["y"]), (end["x"], end["y"])])
        if max(distance_u, distance_v) > cutoff_m and geom.length > 0:
            from_u = distance_u <= distance_v
            start_node = u if from_u else v
            start_point = shapely.Point(graph.nodes[start_node]["x"], graph.nodes[start_node]["y"])
            coordinates = list(geom.coords)
            if shapely.Point(coordinates[-1]).distance(start_point) < shapely.Point(coordinates[0]).distance(start_point):
                geom = LineString(coordinates[::-1])
            reachable_length = min(float(geom.length), max(0.0, cutoff_m - nearest_distance))
            geom = substring(geom, 0.0, reachable_length)
        key = geom.wkb
        if key not in seen:
            seen.add(key)
            geometries.append(geom)
    return geometries


def polygon_from_distances(graph, distances: dict, *, cutoff_m: float, buffer_m: float):
    lines = reachable_edge_geometries(graph, distances, cutoff_m=cutoff_m)
    if not lines:
        nodes = [node for node, distance in distances.items() if distance <= cutoff_m and node in graph.nodes]
        if not nodes:
            return None
        points = [shapely.Point(graph.nodes[node]["x"], graph.nodes[node]["y"]) for node in nodes]
        return unary_union(points).buffer(buffer_m)
    return dissolve_buffered_lines(lines, buffer_m)


def dissolve_buffered_lines(lines: list, buffer_m: float, *, tile_m: float = 2_000.0, small: int = 5_000):
    """Buffer lines and dissolve them.

    For many lines (a city-wide multi-stop catchment), buffering one merged
    multiline is very slow and memory hungry in GEOS. Buffering each line and
    dissolving tile by tile gives the same polygon, because buffering
    distributes over union.
    """
    if len(lines) <= small:
        return unary_union(lines).buffer(buffer_m)
    buffered = shapely.buffer(np.asarray(lines, dtype=object), buffer_m, quad_segs=8)
    centroids = shapely.centroid(buffered)
    keys = pd.MultiIndex.from_arrays(
        [np.floor(shapely.get_x(centroids) / tile_m), np.floor(shapely.get_y(centroids) / tile_m)]
    )
    tiles = [shapely.union_all(buffered[positions]) for positions in pd.Series(range(len(buffered)), index=keys).groupby(level=[0, 1]).agg(list)]
    return shapely.union_all(np.asarray(tiles, dtype=object))


def build_station_isochrones(
    graph,
    stations: gpd.GeoDataFrame,
    *,
    cutoff_m: float = 1125.0,
    buffer_m: float = 25.0,
    existing: gpd.GeoDataFrame | None = None,
    checkpoint_every: int = 10,
    progress_callback: Callable[[gpd.GeoDataFrame], None] | None = None,
    csr: CsrGraph | None = None,
    batch_size: int = 25,
) -> gpd.GeoDataFrame:
    """Build one network-distance catchment per station.

    Distances come from scipy's Dijkstra on a CSR copy of the graph, run for
    ``batch_size`` stations at a time to bound memory.
    """
    graph_crs = graph.graph.get("crs")
    if stations.empty:
        return gpd.GeoDataFrame(columns=["station_id", "geometry"], geometry="geometry", crs=graph_crs)
    nodes = nearest_nodes(graph, stations)
    cached = (
        existing.to_crs(graph_crs)
        if existing is not None and not existing.empty and existing.crs != graph_crs
        else existing
    )
    rows = [] if cached is None or cached.empty else cached[["station_id", "geometry"]].to_dict("records")
    completed_ids = {row["station_id"] for row in rows}
    pending = [
        (station_id, node)
        for station_id, node in zip(stations["station_id"], nodes)
        if station_id not in completed_ids
    ]
    if pending and csr is None:
        csr = CsrGraph(graph)
    pending_since_checkpoint = 0
    total = len(stations)
    for start in range(0, len(pending), batch_size):
        batch = pending[start : start + batch_size]
        batch_distances = csr.distances_from([node for _, node in batch], cutoff_m=cutoff_m)
        for (station_id, _), distances in zip(batch, batch_distances):
            polygon = polygon_from_distances(graph, distances, cutoff_m=cutoff_m, buffer_m=buffer_m)
            if polygon is not None and not polygon.is_empty:
                rows.append({"station_id": station_id, "geometry": polygon})
            completed_ids.add(station_id)
            pending_since_checkpoint += 1
        if pending_since_checkpoint >= checkpoint_every:
            result = gpd.GeoDataFrame(rows, geometry="geometry", crs=graph_crs)
            if progress_callback is not None:
                progress_callback(result)
            print(f"Walking catchments: {len(completed_ids)}/{total}", flush=True)
            pending_since_checkpoint = 0
    result = gpd.GeoDataFrame(rows, geometry="geometry", crs=graph_crs)
    if progress_callback is not None and pending_since_checkpoint:
        progress_callback(result)
    print(f"Walking catchments: {len(completed_ids)}/{total}", flush=True)
    return result


def build_combined_isochrone(
    graph,
    stations: gpd.GeoDataFrame,
    *,
    cutoff_m: float = 1125.0,
    buffer_m: float = 25.0,
    csr: CsrGraph | None = None,
):
    """Build one dissolved network catchment around many stops at once."""
    if stations.empty:
        return None
    nodes = nearest_nodes(graph, stations)
    csr = csr or CsrGraph(graph)
    distances = csr.multi_source_distances(nodes, cutoff_m=cutoff_m)
    return polygon_from_distances(graph, distances, cutoff_m=cutoff_m, buffer_m=buffer_m)
