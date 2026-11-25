"""DEGURBA-style urban-centre classification on population grid cells.

This adapts the Degree of Urbanisation rules (Dijkstra and Poelman; GHSL 2023)
to the study H3 cells:

1. Urban-centre candidate cells have land population density of at least
   ``centre_density_min`` or footprint built-up share of at least
   ``builtup_share_min``.
2. Edge-adjacent candidates form clusters; clusters with at least
   ``centre_min_population`` residents become urban centres.
3. Gaps are filled iteratively: a non-water cell with at least
   ``gap_fill_min_neighbors`` neighbours in a centre joins it.
4. Enclosed non-water holes up to ``max_hole_area_sqkm`` are filled.
5. Remaining cells with density of at least ``cluster_density_min`` form
   urban clusters when their contiguous population reaches
   ``cluster_min_population``.

H3 resolution 8 cells (about 0.74 sq km) are smaller than the 1 sq km DEGURBA
grid, so density thresholds are slightly stricter at this resolution.
"""

from __future__ import annotations

from collections import deque

import geopandas as gpd
import pandas as pd
from shapely.geometry import Polygon
from shapely.ops import unary_union

from .geometry import polygon_parts


URBAN_CENTRE = "urban_centre"
URBAN_CLUSTER = "urban_cluster"
RURAL = "rural"


def _components(members: set, adjacency: dict) -> list[set]:
    remaining = set(members)
    components = []
    while remaining:
        start = remaining.pop()
        component = {start}
        queue = deque([start])
        while queue:
            current = queue.popleft()
            for neighbor in adjacency.get(current, set()):
                if neighbor in remaining:
                    remaining.discard(neighbor)
                    component.add(neighbor)
                    queue.append(neighbor)
        components.append(component)
    return components


def _fill_gaps(centre: set, adjacency: dict, water: set, min_neighbors: int) -> set:
    centre = set(centre)
    while True:
        frontier = {
            neighbor
            for cid in centre
            for neighbor in adjacency.get(cid, set())
            if neighbor not in centre and neighbor not in water
        }
        added = {
            cid
            for cid in frontier
            if sum(1 for neighbor in adjacency.get(cid, set()) if neighbor in centre) >= min_neighbors
        }
        if not added:
            return centre
        centre |= added


def _fill_holes(
    cells: gpd.GeoDataFrame,
    centre: set,
    adjacency: dict,
    water: set,
    max_hole_area_sqkm: float,
) -> set:
    if not centre or max_hole_area_sqkm <= 0:
        return set(centre)
    geometry_by_id = cells.set_index("cell_id").geometry
    holes = []
    for component in _components(centre, adjacency):
        union = unary_union(list(geometry_by_id.loc[list(component)]))
        for polygon in polygon_parts(union):
            for ring in polygon.interiors:
                hole = Polygon(ring)
                if hole.area <= max_hole_area_sqkm * 1_000_000:
                    holes.append(hole)
    if not holes:
        return set(centre)
    hole_frame = gpd.GeoDataFrame(geometry=holes, crs=cells.crs)
    candidates = cells[~cells["cell_id"].isin(centre) & ~cells["cell_id"].isin(water)][["cell_id", "geometry"]].copy()
    if candidates.empty:
        return set(centre)
    candidates["geometry"] = candidates.geometry.representative_point()
    inside = gpd.sjoin(candidates, hole_frame, how="inner", predicate="within")
    return set(centre) | set(inside["cell_id"])


def classify_degurba(
    cells: gpd.GeoDataFrame,
    adjacency: dict[int, set[int]],
    *,
    centre_density_min: float = 1500.0,
    cluster_density_min: float = 300.0,
    builtup_share_min: float = 0.20,
    centre_min_population: float = 50_000.0,
    cluster_min_population: float = 5_000.0,
    gap_fill_min_neighbors: int = 4,
    max_hole_area_sqkm: float = 15.0,
) -> gpd.GeoDataFrame:
    """Return cells with ``degurba_class`` and ``degurba_centre_id`` columns."""
    result = cells.copy()
    ids = result["cell_id"].to_list()
    population = dict(zip(ids, result["population"].fillna(0.0)))
    density = dict(zip(ids, result["population_density"].fillna(0.0)))
    builtup = dict(zip(ids, result.get("built_up_coverage", pd.Series(0.0, index=result.index)).fillna(0.0)))
    is_water = result.get("is_water", pd.Series(False, index=result.index)).fillna(False).astype(bool)
    water = set(result.loc[is_water, "cell_id"])

    dense = {
        cid
        for cid in ids
        if cid not in water and (density[cid] >= centre_density_min or builtup[cid] >= builtup_share_min)
    }
    centre = set().union(
        *[
            component
            for component in _components(dense, adjacency)
            if sum(population[cid] for cid in component) >= centre_min_population
        ]
    )
    centre = _fill_gaps(centre, adjacency, water, gap_fill_min_neighbors)
    centre = _fill_holes(result, centre, adjacency, water, max_hole_area_sqkm)

    centre_ids = {}
    ranked = sorted(_components(centre, adjacency), key=lambda c: -sum(population[cid] for cid in c))
    for label, component in enumerate(ranked):
        for cid in component:
            centre_ids[cid] = label

    semi_dense = {cid for cid in ids if cid not in water and (density[cid] >= cluster_density_min or cid in centre)}
    cluster = set().union(
        *[
            component
            for component in _components(semi_dense, adjacency)
            if sum(population[cid] for cid in component) >= cluster_min_population
        ]
    )

    result["degurba_class"] = [
        URBAN_CENTRE if cid in centre else URBAN_CLUSTER if cid in cluster else RURAL for cid in ids
    ]
    result["degurba_centre_id"] = [centre_ids.get(cid, -1) for cid in ids]
    return result


def select_study_centres(
    classified: gpd.GeoDataFrame,
    *,
    centre_point=None,
    keep_other_centres: bool = False,
) -> set[int]:
    """Choose which urban-centre components form the study area.

    The primary centre contains (or is nearest to) ``centre_point``; without
    a point it is the most populous centre.
    """
    centres = classified[classified["degurba_centre_id"] >= 0]
    if centres.empty:
        return set()
    if keep_other_centres:
        return set(centres["cell_id"])
    if centre_point is None:
        primary = 0
    else:
        containing = centres[centres.geometry.contains(centre_point)]
        source = containing if not containing.empty else centres
        distances = source.geometry.distance(centre_point)
        primary = int(source.loc[distances.idxmin(), "degurba_centre_id"])
    return set(centres.loc[centres["degurba_centre_id"] == primary, "cell_id"])


def run_degurba_method(
    cells: gpd.GeoDataFrame,
    adjacency: dict[int, set[int]],
    growth,
    *,
    centre_point=None,
) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """Classify cells and return ``(classified_cells, study_urban_cells)``."""
    classified = classify_degurba(
        cells,
        adjacency,
        centre_density_min=growth.centre_density_min,
        cluster_density_min=growth.cluster_density_min,
        builtup_share_min=growth.builtup_share_min,
        centre_min_population=growth.centre_min_population,
        cluster_min_population=growth.cluster_min_population,
        gap_fill_min_neighbors=growth.gap_fill_min_neighbors,
        max_hole_area_sqkm=growth.max_hole_area_sqkm,
    )
    keep = select_study_centres(
        classified,
        centre_point=centre_point,
        keep_other_centres=growth.keep_other_centres,
    )
    classified["in_study_area"] = classified["cell_id"].isin(keep)
    urban = classified[classified["in_study_area"]].copy().reset_index(drop=True)
    return classified, urban
