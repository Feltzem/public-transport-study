"""Configuration for OSM rail, walking catchments, GTFS, and r5py."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
import json
from pathlib import Path
from typing import Any

from urban_area.config import city_slug, infer_metric_crs_from_city_name


@dataclass(frozen=True)
class TransportConfig:
    city_name: str
    city_slug: str | None = None
    metric_crs: str | None = None
    data_dir: Path = Path("./data")
    cache_dir: Path = Path("./cache")
    boundaries_path: Path = Path("D:/Public Transport Study/Input Data/Boundaries/Boundaries.gpkg")
    boundaries_layer: str = "cities_study"
    population_gpkg: Path = Path("./data/population_density.gpkg")
    population_layer: str = "population_density_cities_used"
    building_gpkg: Path = Path("./data/building_footprints.gpkg")
    building_layer: str | None = None
    urban_areas_gpkg: Path = Path("./data/urban_areas.gpkg")
    rail_gpkg: Path = Path("./data/rail_lines.gpkg")
    isochrone_gpkg: Path = Path("./data/station_isochrones.gpkg")
    network_type: str = "walk"
    walking_distance_m: float = 1125.0
    isochrone_buffer_m: float = 25.0
    min_station_population: float = 500.0
    min_station_urban_share: float = 0.25
    rail_modes: list[str] = field(default_factory=lambda: ["rail", "subway", "light_rail", "tram"])
    # "gtfs" builds stations from GTFS parent stations; "osm" uses OSM
    # station features matched to GTFS by mode, name, and distance.
    station_source: str = "gtfs"
    station_merge_distance_m: float = 150.0
    bus_walking_distance_m: float = 500.0
    all_modes: list[str] = field(default_factory=lambda: ["rail", "metro", "tram", "bus", "ferry", "funicular", "cable"])
    gtfs_feed_urls: list[str] = field(default_factory=list)
    # Optional request headers per configured feed URL. Values are
    # environment-variable templates (e.g. "${DELIJN_API_KEY}"), expanded
    # only at download time, so secrets never reach config or provenance.
    gtfs_feed_headers: dict[str, dict[str, str]] = field(default_factory=dict)
    gtfs_service_date: str | None = None
    gtfs_window: tuple[int, int] = (7, 9)
    frequent_dep_per_hr: float = 6.0
    gtfs_match_tolerance_m: float = 500.0
    gtfs_min_name_similarity: float = 0.45
    centre_lon: float | None = None
    centre_lat: float | None = None
    centre_label: str | None = None
    osm_pbf: Path | None = None

    def resolved_slug(self) -> str:
        return self.city_slug or city_slug(self.city_name)

    def resolved_metric_crs(self) -> str:
        crs = self.metric_crs or infer_metric_crs_from_city_name(self.city_name)
        if crs is None:
            raise ValueError(f"No metric CRS configured for {self.city_name}")
        return crs

    def resolved_building_layer(self) -> str:
        return self.building_layer or f"{self.resolved_slug()}_buildings"

    def urban_cells_layer(self) -> str:
        return f"urban_cells_{self.resolved_slug()}_growing_refined"

    def analysis_cells_layer(self) -> str:
        return f"population_density_with_buildings_roads_{self.resolved_slug()}"

    def resolved_service_date(self) -> date:
        """Configured representative service date, else the next Tuesday."""
        if self.gtfs_service_date:
            return date.fromisoformat(self.gtfs_service_date)
        from .gtfs import typical_tuesday

        return typical_tuesday()

    def walk_minutes(self) -> float:
        """Walking time implied by ``walking_distance_m`` at 4.5 km/h."""
        return self.walking_distance_m / 75.0

    def walk_graph_path(self) -> Path:
        crs_key = self.resolved_metric_crs().replace(":", "")
        return self.data_dir / "walkable_networks" / f"{self.resolved_slug()}_{self.network_type}_{crs_key}.graphml"


_PATH_FIELDS = {
    "data_dir",
    "cache_dir",
    "boundaries_path",
    "population_gpkg",
    "building_gpkg",
    "urban_areas_gpkg",
    "rail_gpkg",
    "isochrone_gpkg",
    "osm_pbf",
}


def load_transport_config(path: str | Path, city_name: str) -> TransportConfig:
    """Load transport settings from the shared multi-city JSON file."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    try:
        raw: dict[str, Any] = dict(payload["cities"][city_name])
    except KeyError as exc:
        raise KeyError(f"City {city_name!r} is not present in {path}") from exc
    transport = dict(raw.pop("transport", {}))
    gtfs = dict(raw.pop("gtfs", {}))
    travel_time = dict(raw.pop("travel_time", {}))
    allowed_base = {
        "city_slug",
        "metric_crs",
        "data_dir",
        "boundaries_path",
        "boundaries_layer",
        "population_gpkg",
        "population_layer",
        "building_gpkg",
        "building_layer",
        "urban_areas_gpkg",
    }
    values = {key: value for key, value in raw.items() if key in allowed_base}
    values.update(transport)
    if "feed_urls" in gtfs:
        values["gtfs_feed_urls"] = gtfs["feed_urls"]
    if "feed_headers" in gtfs:
        values["gtfs_feed_headers"] = {url: dict(headers) for url, headers in gtfs["feed_headers"].items()}
    if "match_tolerance_m" in gtfs:
        values["gtfs_match_tolerance_m"] = gtfs["match_tolerance_m"]
    if "min_name_similarity" in gtfs:
        values["gtfs_min_name_similarity"] = gtfs["min_name_similarity"]
    if "service_date" in gtfs:
        values["gtfs_service_date"] = gtfs["service_date"]
    if "window" in gtfs:
        values["gtfs_window"] = tuple(int(hour) for hour in gtfs["window"])
    if "frequent_dep_per_hr" in gtfs:
        values["frequent_dep_per_hr"] = float(gtfs["frequent_dep_per_hr"])
    for key in ["centre_lon", "centre_lat", "centre_label"]:
        if key in raw:
            values[key] = raw[key]
    for key in ["centre_lon", "centre_lat", "osm_pbf"]:
        if key in travel_time:
            values[key] = travel_time[key]
    for key in _PATH_FIELDS:
        if key in values and values[key] is not None:
            values[key] = Path(values[key])
    return TransportConfig(city_name=city_name, **values)
