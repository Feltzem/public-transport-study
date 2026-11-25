"""Region-growing urban-area detection."""

from __future__ import annotations

from collections import deque

import geopandas as gpd

from .geometry import build_adjacency_graph, component_labels


STRICTNESS_PARAMS = {
    "strict": {
        "seed_q": 0.92,
        "score_min": 0.40,
        "pop_min": 0.15,
        "bldg_min": 0.12,
        "road_min": 0.12,
        "pop_strong": 0.55,
        "bldg_strong": 0.55,
        "neighbor_relax": 0.02,
    },
    "medium": {
        "seed_q": 0.88,
        "score_min": 0.30,
        "pop_min": 0.10,
        "bldg_min": 0.08,
        "road_min": 0.08,
        "pop_strong": 0.45,
        "bldg_strong": 0.45,
        "neighbor_relax": 0.04,
    },
    "loose": {
        "seed_q": 0.80,
        "score_min": 0.24,
        "pop_min": 0.06,
        "bldg_min": 0.05,
        "road_min": 0.05,
        "pop_strong": 0.35,
        "bldg_strong": 0.35,
        "neighbor_relax": 0.06,
    },
}


def find_seed_clusters(
    cells,
    adjacency,
    *,
    seed_quantile: float = 0.90,
    min_cluster_size: int = 3,
    min_evidence_norm: float = 0.002,
) -> list[set[int]]:
    threshold = cells["urban_score_base"].quantile(seed_quantile)
    has_morphological_evidence = (
        (cells["pop_norm"] >= min_evidence_norm)
        | (cells["building_signal_norm"] >= min_evidence_norm)
    )
    candidates = set(
        cells.loc[
            (cells["urban_score_base"] >= threshold)
            & (cells["urban_score_base"] > 0)
            & has_morphological_evidence,
            "cell_id",
        ]
    )
    if not candidates:
        fallback = cells.loc[(cells["urban_score_base"] > 0) & has_morphological_evidence]
        if fallback.empty:
            return []
        best_idx = fallback["urban_score_base"].idxmax()
        return [{int(cells.loc[best_idx, "cell_id"])}]

    clusters: list[set[int]] = []
    visited = set()
    for start in candidates:
        if start in visited:
            continue
        cluster = set()
        q = deque([start])
        visited.add(start)
        while q:
            cid = q.popleft()
            cluster.add(cid)
            for neighbor in adjacency.get(cid, set()):
                if neighbor in candidates and neighbor not in visited:
                    visited.add(neighbor)
                    q.append(neighbor)
        if len(cluster) >= min_cluster_size:
            clusters.append(cluster)
    if clusters:
        return clusters
    return [candidates]


def _components_to_keep(component_stats, *, keep_largest_component: bool, min_population: float, min_area_sqkm: float, min_built_up_area_sqm: float) -> set[int]:
    if component_stats.empty:
        return set()
    if keep_largest_component:
        return {component_stats["area_sqkm"].idxmax()}
    thresholds = [
        ("population", min_population),
        ("area_sqkm", min_area_sqkm),
        ("built_up_area_sqm", min_built_up_area_sqm),
    ]
    active = [(col, threshold) for col, threshold in thresholds if threshold > 0]
    if not active:
        return set(component_stats.index)
    keep = set()
    for col, threshold in active:
        keep.update(component_stats.loc[component_stats[col] >= threshold].index)
    if not keep:
        keep = {component_stats["area_sqkm"].idxmax()}
    return keep


def run_growing_method(
    cells: gpd.GeoDataFrame,
    *,
    urban_strictness: str = "medium",
    water_guard: bool = True,
    multi_seed: bool = True,
    min_seed_cluster_size: int = 2,
    adjacency_mode: str = "edge",
    min_shared_boundary_m: float = 1.0,
    adjacency: dict[int, set[int]] | None = None,
    keep_largest_component: bool = True,
    min_component_population: float = 0.0,
    min_component_area_sqkm: float = 0.0,
    min_component_built_up_area_sqm: float = 0.0,
) -> gpd.GeoDataFrame:
    """Detect urban cells using multi-seed region growing."""
    if urban_strictness not in STRICTNESS_PARAMS:
        raise ValueError(f"Unknown strictness {urban_strictness!r}; expected one of {sorted(STRICTNESS_PARAMS)}")

    density = cells[cells.geometry.is_valid].copy().reset_index(drop=True)
    if "cell_id" not in density.columns:
        density["cell_id"] = range(len(density))
    params = STRICTNESS_PARAMS[urban_strictness]

    density["qual_score"] = (
        0.50 * density["pop_norm"] + 0.40 * density["building_signal_norm"] + 0.10 * density.get("road_norm", 0)
    )
    if "is_water" not in density:
        density["is_water"] = False
    if adjacency is None:
        adjacency = build_adjacency_graph(
            density,
            id_col="cell_id",
            mode=adjacency_mode,
            min_shared_boundary_m=min_shared_boundary_m,
        )
    lookup = density.set_index("cell_id")[[
        "pop_norm",
        "building_signal_norm",
        "road_norm",
        "qual_score",
        "is_water",
    ]].to_dict(orient="index")
    eps_pop = max(5.0 / (float(density["population"].max()) or 1.0), 0.002)
    eps_building = 0.002
    eps_road = 0.002

    if multi_seed:
        seed_clusters = find_seed_clusters(
            density,
            adjacency,
            seed_quantile=params["seed_q"],
            min_cluster_size=min_seed_cluster_size,
        )
        if not seed_clusters:
            return gpd.GeoDataFrame(columns=density.columns, geometry="geometry", crs=density.crs)
        urban_ids = set().union(*seed_clusters)
        q = deque(urban_ids)
    else:
        eligible = density[~density["is_water"]]
        if eligible.empty:
            return gpd.GeoDataFrame(columns=density.columns, geometry="geometry", crs=density.crs)
        seed_idx = eligible["urban_score_base"].idxmax()
        seed_id = density.loc[seed_idx, "cell_id"]
        urban_ids = {seed_id}
        q = deque([seed_id])

    while q:
        current = q.popleft()
        for neighbor in adjacency.get(current, set()):
            if neighbor in urban_ids:
                continue
            row = lookup[neighbor]
            if row["is_water"]:
                continue
            if water_guard and row["pop_norm"] < eps_pop and row["building_signal_norm"] < eps_building:
                continue
            neighbor_set = adjacency.get(neighbor, set())
            urban_share = len(neighbor_set & urban_ids) / len(neighbor_set) if neighbor_set else 0.0
            relax = params["neighbor_relax"] * min(1.0, max(0.0, (urban_share - 0.5) * 2))
            score_min = max(0.0, params["score_min"] - relax)
            strong_population = row["pop_norm"] >= params["pop_strong"]
            strong_built_form = row["building_signal_norm"] >= params["bldg_strong"]
            balanced_combo = row["qual_score"] >= score_min and (
                (
                    row["pop_norm"] >= params["pop_min"]
                    and row["building_signal_norm"] >= params["bldg_min"]
                )
                or (
                    row["building_signal_norm"] >= params["bldg_min"]
                    and row["road_norm"] >= params["road_min"]
                )
                or (
                    row["pop_norm"] >= params["pop_min"]
                    and row["road_norm"] >= params["road_min"]
                    and row["building_signal_norm"] >= eps_building
                )
            )
            if strong_population or strong_built_form or balanced_combo:
                urban_ids.add(neighbor)
                q.append(neighbor)

    holes = 0
    for cid in density["cell_id"]:
        if cid in urban_ids:
            continue
        neighbors = adjacency.get(cid, set())
        if len(neighbors) >= 3 and neighbors.issubset(urban_ids):
            row = lookup[cid]
            if row["is_water"]:
                continue
            if row["pop_norm"] >= eps_pop or row["building_signal_norm"] >= eps_building:
                urban_ids.add(cid)
                holes += 1

    if not urban_ids:
        return gpd.GeoDataFrame(columns=density.columns, geometry="geometry", crs=density.crs)

    urban = density[density["cell_id"].isin(urban_ids)].copy()
    if len(urban) > 1:
        urban_adj = {
            cid: {n for n in adjacency.get(cid, set()) if n in urban_ids}
            for cid in urban["cell_id"]
        }
        urban["component"] = component_labels(urban, urban_adj, id_col="cell_id")
        component_stats = urban.groupby("component").agg(
            population=("population", "sum"),
            built_up_area_sqm=("building_area_sqm", "sum"),
        )
        component_stats["area_sqkm"] = urban.groupby("component").geometry.apply(lambda s: s.area.sum() / 1_000_000)
        keep_components = _components_to_keep(
            component_stats,
            keep_largest_component=keep_largest_component,
            min_population=min_component_population,
            min_area_sqkm=min_component_area_sqkm,
            min_built_up_area_sqm=min_component_built_up_area_sqm,
        )
        urban = urban[urban["component"].isin(keep_components)].copy()

    urban["growth_holes_added"] = holes
    return urban.drop(columns=["component", "qual_score"], errors="ignore").reset_index(drop=True)


def detect_urban_cells(
    analysis_cells: gpd.GeoDataFrame,
    growth,
    *,
    adjacency: dict[int, set[int]],
    centre_point=None,
) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """Run the configured urban-detection method.

    Returns ``(analysis_cells, urban_cells)``; the DEGURBA method also adds
    ``degurba_class`` and ``in_study_area`` to the analysis cells.
    """
    if growth.method == "degurba":
        from .degurba import run_degurba_method

        return run_degurba_method(analysis_cells, adjacency, growth, centre_point=centre_point)
    if growth.method != "score":
        raise ValueError(f"Unknown growth method {growth.method!r}; expected 'degurba' or 'score'")
    urban = run_growing_method(
        analysis_cells,
        urban_strictness=growth.strictness,
        water_guard=growth.water_guard,
        multi_seed=growth.use_multi_seed,
        min_seed_cluster_size=growth.min_seed_cluster_size,
        keep_largest_component=growth.keep_largest_component,
        min_component_population=growth.min_component_population,
        min_component_area_sqkm=growth.min_component_area_sqkm,
        min_component_built_up_area_sqm=growth.min_component_built_up_area_sqm,
        adjacency=adjacency,
    )
    return analysis_cells, urban


def component_retention_kwargs(growth) -> dict[str, object]:
    """Component-retention settings applied after edge refinement."""
    if growth.method == "degurba":
        return {
            "keep_largest_component": not growth.keep_other_centres,
            "min_component_population": growth.cluster_min_population if growth.keep_other_centres else 0.0,
            "min_component_area_sqkm": 0.0,
            "min_component_built_up_area_sqm": 0.0,
        }
    return {
        "keep_largest_component": growth.keep_largest_component,
        "min_component_population": growth.min_component_population,
        "min_component_area_sqkm": growth.min_component_area_sqkm,
        "min_component_built_up_area_sqm": growth.min_component_built_up_area_sqm,
    }
