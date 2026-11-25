"""OSM rail-line and station collection."""

from __future__ import annotations

import geopandas as gpd
import pandas as pd

from urban_area.catchment import filter_stations_by_catchment_population
from urban_area.geometry import clean_union
from urban_area.schemas import LINE_TYPES, POINT_TYPES, POLYGON_TYPES


def _first(value) -> str | None:
    if isinstance(value, (list, tuple, set)):
        return str(next(iter(value), "")) or None
    if value is None or (not isinstance(value, (list, tuple, set)) and pd.isna(value)):
        return None
    return str(value)


def _rail_mode(row) -> str:
    values = [_first(row.get(column)) for column in ["railway", "route", "station", "public_transport"]]
    text = " ".join(value.lower() for value in values if value)
    for mode in ["subway", "light_rail", "tram", "monorail", "rail"]:
        if mode in text:
            return mode
    return "rail"


def query_osm_rail(
    boundary_metric: gpd.GeoDataFrame,
    metric_crs: str,
    *,
    rail_modes: list[str],
) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """Query OSM for rail infrastructure and normalise its schema."""
    import osmnx as ox

    polygon = clean_union(boundary_metric.to_crs("EPSG:4326"))
    tags = {
        "railway": list(dict.fromkeys(rail_modes + ["station", "halt", "tram_stop"])),
    }
    raw = ox.features_from_polygon(polygon, tags=tags)
    if raw.empty:
        empty_lines = gpd.GeoDataFrame(columns=["city", "mode", "name", "geometry"], geometry="geometry", crs=metric_crs)
        empty_stations = gpd.GeoDataFrame(columns=["station_id", "city", "mode", "name", "geometry"], geometry="geometry", crs=metric_crs)
        return empty_lines, empty_stations
    raw = raw.to_crs(metric_crs)
    raw = raw[raw.geometry.notna() & ~raw.geometry.is_empty & raw.geometry.is_valid].copy()
    raw["mode"] = raw.apply(_rail_mode, axis=1)
    raw["name"] = raw.get("name", pd.Series(index=raw.index, dtype=object)).apply(_first)

    lines = raw[raw.geom_type.isin(LINE_TYPES) & raw["mode"].isin(rail_modes)].copy()
    lines = lines[["mode", "name", "geometry"]].drop_duplicates(subset=["geometry"]).reset_index(drop=True)

    railway_values = raw.get("railway", pd.Series(index=raw.index, dtype=object)).apply(_first)
    station_tag = railway_values.isin(
        ["station", "halt", "tram_stop"]
    )
    public_transport_tag = raw.get("public_transport", pd.Series(index=raw.index, dtype=object)).apply(_first).isin(
        ["station", "stop_position", "platform"]
    )
    stations = raw[(station_tag | public_transport_tag) & raw.geom_type.isin(POINT_TYPES | POLYGON_TYPES)].copy()
    non_point_mask = stations.geom_type != "Point"
    stations.loc[non_point_mask, "geometry"] = stations.loc[non_point_mask].geometry.centroid
    stations["osm_railway"] = railway_values.loc[stations.index]
    stations = stations[["mode", "name", "osm_railway", "geometry"]].drop_duplicates(subset=["geometry"]).reset_index(drop=True)
    stations["station_id"] = range(len(stations))
    return lines, stations[["station_id", "mode", "name", "osm_railway", "geometry"]]


def prefilter_stations_by_possible_urban_reach(
    stations: gpd.GeoDataFrame,
    urban_cells: gpd.GeoDataFrame,
    *,
    walking_distance_m: float,
) -> gpd.GeoDataFrame:
    """Discard only stations too far away to reach any urban cell.

    This is a geometric impossibility filter, not the station-in-polygon rule
    rejected by the methodology. Final inclusion still uses walking-catchment
    population.
    """
    if stations.empty or urban_cells.empty:
        return stations.iloc[0:0].copy()
    urban_reach_envelope = clean_union(urban_cells.to_crs(stations.crs)).buffer(walking_distance_m)
    result = stations[stations.geometry.intersects(urban_reach_envelope)].copy()
    return result.reset_index(drop=True)


def filter_rail_stations(
    stations: gpd.GeoDataFrame,
    catchments: gpd.GeoDataFrame,
    urban_cells: gpd.GeoDataFrame,
    *,
    all_population_cells: gpd.GeoDataFrame,
    buildings: gpd.GeoDataFrame | None,
    min_population: float,
    min_urban_share: float,
) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    return filter_stations_by_catchment_population(
        stations,
        catchments,
        urban_cells,
        all_population_cells=all_population_cells,
        min_population=min_population,
        min_urban_share=min_urban_share,
        method="auto",
        buildings=buildings,
    )
