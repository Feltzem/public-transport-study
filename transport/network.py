"""Walking-network caching and memory-safe shortest paths."""

from __future__ import annotations

import pickle
from pathlib import Path
from uuid import uuid4

import geopandas as gpd
import networkx as nx
import numpy as np
from pyproj import CRS

from urban_area.geometry import clean_union


def _binary_graph_path(cache_path: Path) -> Path:
    return cache_path.with_suffix(cache_path.suffix + ".pickle")


def _write_binary_graph(graph, cache_path: Path) -> None:
    binary_path = _binary_graph_path(cache_path)
    temporary = binary_path.with_suffix(binary_path.suffix + ".tmp")
    with temporary.open("wb") as stream:
        pickle.dump(graph, stream, protocol=pickle.HIGHEST_PROTOCOL)
    temporary.replace(binary_path)


def load_or_download_walk_graph(
    boundary_metric: gpd.GeoDataFrame,
    *,
    cache_path: Path,
    metric_crs: str,
    network_type: str = "walk",
    force: bool = False,
):
    """Load a graph cache keyed by city/CRS/network type, or query OSM."""
    import osmnx as ox

    binary_path = _binary_graph_path(cache_path)
    if binary_path.exists() and not force and (
        not cache_path.exists() or binary_path.stat().st_mtime_ns >= cache_path.stat().st_mtime_ns
    ):
        with binary_path.open("rb") as stream:
            graph = pickle.load(stream)
        graph_crs = graph.graph.get("crs")
        if graph_crs is None or CRS.from_user_input(graph_crs) != CRS.from_user_input(metric_crs):
            raise ValueError(f"Walk graph binary cache CRS {graph_crs!r} does not match {metric_crs!r}: {binary_path}")
        return graph
    if cache_path.exists() and not force:
        graph = ox.load_graphml(cache_path)
        graph_crs = graph.graph.get("crs")
        if graph_crs is None or CRS.from_user_input(graph_crs) != CRS.from_user_input(metric_crs):
            raise ValueError(f"Walk graph cache CRS {graph_crs!r} does not match {metric_crs!r}: {cache_path}")
        _write_binary_graph(graph, cache_path)
        return graph
    polygon = clean_union(boundary_metric.to_crs("EPSG:4326"))
    graph = ox.graph_from_polygon(polygon, network_type=network_type, retain_all=True, simplify=True)
    graph = ox.project_graph(graph, to_crs=metric_crs)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    ox.save_graphml(graph, cache_path)
    _write_binary_graph(graph, cache_path)
    return graph


class CsrGraph:
    """A graph's walking lengths as a scipy CSR matrix plus node lookups."""

    def __init__(self, graph, *, weight: str = "length"):
        from scipy.sparse import csr_matrix

        self.nodes = list(graph.nodes)
        self.index = {node: position for position, node in enumerate(self.nodes)}
        best: dict[tuple[int, int], float] = {}
        for u, v, data in graph.edges(data=True):
            key = (self.index[u], self.index[v])
            length = float(data.get(weight, 0.0) or 0.0)
            if key not in best or length < best[key]:
                best[key] = length
            if not graph.is_directed():
                reverse = (key[1], key[0])
                if reverse not in best or length < best[reverse]:
                    best[reverse] = length
        rows = np.fromiter((key[0] for key in best), dtype=np.int64, count=len(best))
        cols = np.fromiter((key[1] for key in best), dtype=np.int64, count=len(best))
        # Zero-length edges would vanish from a sparse matrix, so keep a tiny positive weight.
        values = np.maximum(np.fromiter(best.values(), dtype=float, count=len(best)), 1e-6)
        self.matrix = csr_matrix((values, (rows, cols)), shape=(len(self.nodes), len(self.nodes)))

    def distances_from(self, sources: list, *, cutoff_m: float) -> list[dict]:
        """Per-source network distances within ``cutoff_m``."""
        from scipy.sparse.csgraph import dijkstra

        if not sources:
            return []
        indices = [self.index[node] for node in sources]
        matrix = dijkstra(self.matrix, directed=True, indices=indices, limit=cutoff_m)
        results = []
        for row in np.atleast_2d(matrix):
            reachable = np.flatnonzero(np.isfinite(row))
            results.append({self.nodes[position]: float(row[position]) for position in reachable})
        return results

    def multi_source_distances(self, sources: list, *, cutoff_m: float) -> dict:
        """Distance to the nearest of ``sources`` within ``cutoff_m``."""
        from scipy.sparse.csgraph import dijkstra

        if not sources:
            return {}
        indices = sorted({self.index[node] for node in sources})
        row = dijkstra(self.matrix, directed=True, indices=indices, limit=cutoff_m, min_only=True)
        reachable = np.flatnonzero(np.isfinite(row))
        return {self.nodes[position]: float(row[position]) for position in reachable}


def main_component_nodes(graph) -> set:
    """Nodes of the largest weakly connected component."""
    if graph.number_of_nodes() == 0:
        return set()
    components = nx.weakly_connected_components(graph) if graph.is_directed() else nx.connected_components(graph)
    return set(max(components, key=len))


def nearest_nodes(graph, points: gpd.GeoDataFrame, *, main_component_only: bool = True) -> list:
    """Snap points to graph nodes, by default only nodes on the main component.

    Snapping to an isolated footpath fragment would give a station a tiny
    catchment, so fragments are skipped unless ``main_component_only`` is off.
    """
    from scipy.spatial import cKDTree

    graph_crs = graph.graph.get("crs")
    work = points.to_crs(graph_crs) if points.crs != graph_crs else points
    candidates = main_component_nodes(graph) if main_component_only else set(graph.nodes)
    node_ids = [node for node in graph.nodes if node in candidates]
    coordinates = np.array([(graph.nodes[node]["x"], graph.nodes[node]["y"]) for node in node_ids])
    _, positions = cKDTree(coordinates).query(np.column_stack([work.geometry.x, work.geometry.y]))
    return [node_ids[position] for position in np.atleast_1d(positions)]


def super_source_distances(graph, source_nodes: list, *, weight: str = "length") -> dict:
    """Run multi-source Dijkstra by briefly adding one pseudo-node.

    The graph is not copied. The temporary node and edges are always removed,
    even if Dijkstra raises.
    """
    pseudo = f"__transport_super_source_{uuid4().hex}"
    graph.add_node(pseudo)
    try:
        for node in dict.fromkeys(source_nodes):
            graph.add_edge(pseudo, node, **{weight: 0.0})
        distances = nx.single_source_dijkstra_path_length(graph, pseudo, weight=weight)
        distances.pop(pseudo, None)
        return distances
    finally:
        if pseudo in graph:
            graph.remove_node(pseudo)
