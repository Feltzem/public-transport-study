"""OSM water-mask collection and hard urban-cell exclusion."""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import requests
import shapely

from .config import CityConfig
from .geometry import clean_union, vectorized_overlay_measure
from .io import list_layers
from .osm_pbf import city_pbf_path, load_pbf_water
from .schemas import LINE_TYPES, POLYGON_TYPES


WATER_TAGS = {
    "natural": ["water", "bay", "strait", "coastline"],
    "waterway": ["riverbank", "dock", "canal"],
    "landuse": ["reservoir", "basin"],
}

OVERPASS_ENDPOINTS = (
    "https://overpass.private.coffee/api",
    "https://overpass-api.de/api",
)
OVERPASS_MAX_QUERY_AREA_SQM = 100_000_000


def query_osm_water_mask(
    city_boundary_metric: gpd.GeoDataFrame,
    metric_crs: str,
    *,
    coastline_buffer_m: float = 25.0,
) -> gpd.GeoDataFrame:
    """Query OSM water polygons and a narrow coastline exclusion strip."""
    import osmnx as ox

    query_polygon = clean_union(city_boundary_metric.to_crs("EPSG:4326"))
    previous_url = ox.settings.overpass_url
    previous_timeout = ox.settings.requests_timeout
    previous_max_area = ox.settings.max_query_area_size
    endpoints = tuple(dict.fromkeys((*OVERPASS_ENDPOINTS, previous_url)))
    last_error = None
    try:
        ox.settings.requests_timeout = max(previous_timeout, 300)
        ox.settings.max_query_area_size = min(previous_max_area, OVERPASS_MAX_QUERY_AREA_SQM)
        for endpoint in endpoints:
            ox.settings.overpass_url = endpoint
            try:
                features = ox.features_from_polygon(query_polygon, tags=WATER_TAGS)
                break
            except requests.exceptions.RequestException as exc:
                last_error = exc
        else:
            raise RuntimeError(f"All Overpass endpoints failed while querying the water mask: {last_error}") from last_error
    finally:
        ox.settings.overpass_url = previous_url
        ox.settings.requests_timeout = previous_timeout
        ox.settings.max_query_area_size = previous_max_area
    return water_features_to_mask(
        features,
        city_boundary_metric,
        metric_crs,
        coastline_buffer_m=coastline_buffer_m,
    )


def sea_polygons_from_coastline(
    coastlines: gpd.GeoDataFrame,
    boundary,
    *,
    probe_m: float = 1.0,
    min_sea_vote: float = 0.8,
) -> gpd.GeoDataFrame:
    """Polygonise the sea inside ``boundary`` from OSM coastline ways.

    OSM maps the sea only as coastline lines, drawn with land on the left and
    water on the right. The envelope is cut into faces along the coastline;
    each face is voted sea or land by probe points just right (sea) and left
    (land) of every coastline segment. Faces need ``min_sea_vote`` of their
    votes to count as sea, so broken or truncated coastlines fall back to land.
    """
    empty = gpd.GeoDataFrame(columns=["water_source", "geometry"], geometry="geometry", crs=coastlines.crs)
    lines = [geom for geom in coastlines.geometry if geom is not None and not geom.is_empty]
    if not lines or boundary is None or boundary.is_empty:
        return empty
    noded = shapely.union_all([*lines, boundary.boundary])
    faces = [face for face in shapely.get_parts(shapely.polygonize([noded]))
             if face.representative_point().within(boundary)]
    if not faces:
        return empty
    starts, ends = [], []
    for line in lines:
        for part in shapely.get_parts(line):
            coords = np.asarray(part.coords)[:, :2]
            if len(coords) >= 2:
                starts.append(coords[:-1])
                ends.append(coords[1:])
    starts, ends = np.vstack(starts), np.vstack(ends)
    direction = ends - starts
    length = np.hypot(direction[:, 0], direction[:, 1])
    keep = length > 2 * probe_m
    mid = (starts[keep] + ends[keep]) / 2
    unit = direction[keep] / length[keep, None]
    right = np.column_stack([unit[:, 1], -unit[:, 0]]) * probe_m
    weight = np.r_[length[keep], length[keep]]
    probes = gpd.GeoDataFrame(
        {"vote": np.r_[np.ones(len(mid)), np.zeros(len(mid))] * weight, "weight": weight},
        geometry=shapely.points(np.vstack([mid + right, mid - right])),
        crs=coastlines.crs,
    )
    face_frame = gpd.GeoDataFrame({"face": range(len(faces))}, geometry=faces, crs=coastlines.crs)
    joined = gpd.sjoin(probes, face_frame, how="inner", predicate="within").groupby("face")[["vote", "weight"]].sum()
    # Length-weighted share of coastline probes saying "sea".
    votes = joined["vote"] / joined["weight"]
    sea = face_frame[face_frame["face"].map(votes).fillna(0.0) >= min_sea_vote]
    return gpd.GeoDataFrame({"water_source": "osm_sea_from_coastline", "geometry": sea.geometry}, crs=coastlines.crs)


def water_features_to_mask(
    features: gpd.GeoDataFrame,
    city_boundary_metric: gpd.GeoDataFrame,
    metric_crs: str,
    *,
    coastline_buffer_m: float = 25.0,
) -> gpd.GeoDataFrame:
    """Normalize raw OSM water features into polygon exclusions."""
    if features.empty:
        return gpd.GeoDataFrame(columns=["water_source", "geometry"], geometry="geometry", crs=metric_crs)
    features = features.to_crs(metric_crs)
    # Only polygons carrying a water tag are water. Closed coastline rings are
    # islands, and member ways pulled in with a water relation (lake islands
    # tagged natural=wood and similar) are land.
    is_water_tagged = pd.Series(False, index=features.index)
    for key, values in WATER_TAGS.items():
        if key in features:
            is_water_tagged |= features[key].isin([value for value in values if value != "coastline"])
    polygons = features[features.geom_type.isin(POLYGON_TYPES) & is_water_tagged].copy()
    polygons["water_source"] = "osm_water_polygon"
    coast = features[
        features.geom_type.isin(LINE_TYPES)
        & features.get("natural", pd.Series(index=features.index, dtype=object)).eq("coastline")
    ].copy()
    coast["water_source"] = "osm_coastline_buffer"
    sea = sea_polygons_from_coastline(coast, clean_union(city_boundary_metric.to_crs(metric_crs)))
    if not coast.empty and coastline_buffer_m > 0:
        coast["geometry"] = coast.geometry.buffer(coastline_buffer_m)
    water = pd.concat(
        [polygons[["water_source", "geometry"]], coast[["water_source", "geometry"]], sea[["water_source", "geometry"]]],
        ignore_index=True,
    )
    water = gpd.GeoDataFrame(water, geometry="geometry", crs=metric_crs)
    water = water[water.geometry.notna() & ~water.geometry.is_empty & water.geometry.is_valid].copy()
    if water.empty:
        return water
    boundary = clean_union(city_boundary_metric.to_crs(metric_crs))
    water["geometry"] = shapely.intersection(water.geometry.to_numpy(), boundary)
    return water[~water.geometry.is_empty].reset_index(drop=True)


def load_or_query_water_mask(
    config: CityConfig,
    city_boundary_metric: gpd.GeoDataFrame,
    metric_crs: str,
    *,
    query_missing: bool,
) -> gpd.GeoDataFrame:
    """Load a city water mask, querying and caching it when requested."""
    layer = config.water_mask.resolved_layer(config.resolved_slug())
    path = Path(config.water_mask.water_gpkg)
    if path.exists() and layer in list_layers(path):
        return gpd.read_file(path, layer=layer).to_crs(metric_crs)
    if not query_missing:
        raise FileNotFoundError(
            f"Water mask {path}/{layer} is missing; rerun with --query-missing to create it"
        )
    pbf_path = city_pbf_path(config.resolved_slug())
    if pbf_path.exists():
        features = load_pbf_water(pbf_path, city_slug=config.resolved_slug())
        water = water_features_to_mask(
            features,
            city_boundary_metric,
            metric_crs,
            coastline_buffer_m=config.water_mask.coastline_buffer_m,
        )
    else:
        water = query_osm_water_mask(
            city_boundary_metric,
            metric_crs,
            coastline_buffer_m=config.water_mask.coastline_buffer_m,
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    water.to_file(path, layer=layer, driver="GPKG")
    return water


def apply_water_exclusion(
    cells: gpd.GeoDataFrame,
    water_mask: gpd.GeoDataFrame,
    *,
    min_land_fraction: float = 0.10,
    min_building_area_sqm: float = 100.0,
) -> gpd.GeoDataFrame:
    """Annotate land area and flag cells that are effectively open water.

    Partially wet cells keep their full population and building evidence;
    ``add_urban_metrics`` then computes densities on ``land_area_sqm``. Only
    cells with almost no land and no buildings are flagged ``is_water`` and
    have their growth signals zeroed.
    """
    result = cells.copy()
    result["water_fraction"] = 0.0
    result["land_area_sqm"] = result.geometry.area
    result["is_water"] = False
    if result.empty or water_mask.empty:
        return result
    union = clean_union(water_mask.to_crs(result.crs))
    zones = gpd.GeoDataFrame({"water_id": [0], "geometry": [union]}, crs=result.crs)
    pairs = vectorized_overlay_measure(
        zones,
        result,
        feature_id_col="water_id",
        zone_id_col="cell_id",
        measure="area",
    )
    cell_area = result.geometry.area
    water_area = result["cell_id"].map(pairs.groupby("cell_id")["intersection_measure"].sum()).fillna(0.0)
    result["water_fraction"] = (water_area / cell_area.where(cell_area > 0)).clip(0, 1).fillna(0.0)
    result["land_area_sqm"] = (cell_area * (1.0 - result["water_fraction"])).clip(lower=0)
    building_area = result.get("total_building_area_sqm", pd.Series(0.0, index=result.index)).fillna(0.0)
    result["is_water"] = ((1.0 - result["water_fraction"]) < min_land_fraction) & (building_area < min_building_area_sqm)
    signal_columns = [
        "pop_norm",
        "building_signal_norm",
        "road_norm",
        "urban_score_base",
        "urban_score",
    ]
    for column in signal_columns:
        if column in result:
            result.loc[result["is_water"], column] = 0.0
    return result


def clip_cells_to_land(cells: gpd.GeoDataFrame, water_mask: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Subtract water from output cell geometry.

    Population and building area are not scaled: residents and buildings are
    already on the land part of the cell, so area-weighting would lose them.
    """
    if cells.empty or water_mask.empty:
        return cells.copy()
    result = cells.copy()
    water_union = clean_union(water_mask.to_crs(result.crs))
    original_area = result.geometry.area
    result["geometry"] = shapely.difference(result.geometry.to_numpy(), water_union)
    result = result[~result.geometry.is_empty & result.geometry.notna()].copy()
    remaining_fraction = (result.geometry.area / original_area.loc[result.index].where(original_area.loc[result.index] > 0)).clip(0, 1)
    result["water_clipped_fraction"] = 1.0 - remaining_fraction
    result["cell_area_sqm"] = result.geometry.area
    result["cell_area_sqkm"] = result["cell_area_sqm"] / 1_000_000
    result["land_area_sqm"] = result["cell_area_sqm"]
    return result.reset_index(drop=True)
