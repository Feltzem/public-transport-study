"""Configuration helpers for urban-area detection."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import json
import re
import unicodedata
from typing import Any


MAJOR_TRANSPORT_ROAD_VALUES = [
    "motorway",
    "trunk",
    "motorway_link",
    "trunk_link",
]

LOCAL_URBAN_FABRIC_ROAD_VALUES = [
    "primary",
    "secondary",
    "tertiary",
    "unclassified",
    "residential",
    "living_street",
    "primary_link",
    "secondary_link",
    "tertiary_link",
    "service",
]

DEFAULT_ROAD_TAGS = {
    "highway": MAJOR_TRANSPORT_ROAD_VALUES + LOCAL_URBAN_FABRIC_ROAD_VALUES
}

PREFERRED_CITY_CRS = {
    "Oslo": "EPSG:25833",
    "Bergen": "EPSG:25832",
    "Trondheim": "EPSG:25833",
    "Stavanger": "EPSG:25832",
    "Stockholm": "EPSG:3006",
    "Gothenburg": "EPSG:3006",
    "Malmo": "EPSG:3006",
    "Malmö": "EPSG:3006",
    "Copenhagen": "EPSG:25832",
    "Helsinki": "EPSG:3067",
    "London": "EPSG:27700",
    "Manchester": "EPSG:27700",
    "Edinburgh": "EPSG:27700",
    "Paris": "EPSG:2154",
    "Lyon": "EPSG:2154",
    "Marseille": "EPSG:2154",
    "Berlin": "EPSG:25833",
    "Munich": "EPSG:25832",
    "Hamburg": "EPSG:25832",
    "Amsterdam": "EPSG:28992",
    "Madrid": "EPSG:25830",
    "Barcelona": "EPSG:25831",
    "Rome": "EPSG:32633",
    "Milan": "EPSG:32632",
    "New York": "EPSG:32618",
    "Los Angeles": "EPSG:32611",
    "Chicago": "EPSG:32616",
    "Toronto": "EPSG:32617",
    "Vancouver": "EPSG:32610",
}


def city_slug(city_name: str) -> str:
    """Return a stable ASCII slug for layer names and output files."""
    city_part = city_name.split(",")[0].strip()
    normalized = unicodedata.normalize("NFKD", city_part).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", normalized).strip("_").lower()
    if not slug:
        raise ValueError(f"Could not derive slug from city name: {city_name!r}")
    return slug


def infer_metric_crs_from_city_name(city_name: str) -> str | None:
    """Return a preferred projected CRS from the city lookup table."""
    city_part = city_name.split(",")[0].strip()
    return PREFERRED_CITY_CRS.get(city_part)


@dataclass(frozen=True)
class GrowthConfig:
    strictness: str = "medium"
    use_multi_seed: bool = True
    min_seed_cluster_size: int = 2
    water_guard: bool = True
    keep_largest_component: bool = True
    min_component_population: float = 0.0
    min_component_area_sqkm: float = 0.0
    min_component_built_up_area_sqm: float = 0.0


@dataclass(frozen=True)
class RefinementConfig:
    enabled: bool = True
    subdivision_factor: int = 4
    min_building_area_sqm: float = 600.0
    min_road_density_km_per_sqkm: float = 3.0
    require_local_presence: bool = True


@dataclass(frozen=True)
class CityConfig:
    city_name: str
    city_slug: str | None = None
    metric_crs: str | None = None
    data_dir: Path = Path("./data")
    boundaries_path: Path = Path("D:/Public Transport Study/Input Data/Boundaries/Boundaries.gpkg")
    boundaries_layer: str = "cities_used"
    analysis_envelope_buffer_m: float = 0.0
    population_gpkg: Path = Path("./data/population_density.gpkg")
    population_layer: str = "population_density_cities_used"
    urban_areas_gpkg: Path = Path("./data/urban_areas.gpkg")
    building_gpkg: Path = Path("./data/building_footprints.gpkg")
    building_layer: str | None = None
    road_gpkg: Path = Path("./data/road_networks.gpkg")
    road_layer: str | None = None
    building_mode: str = "auto"  # auto, footprint, point, required_footprint
    road_tags: dict[str, list[str]] = field(default_factory=lambda: {k: list(v) for k, v in DEFAULT_ROAD_TAGS.items()})
    local_road_values: list[str] = field(default_factory=lambda: LOCAL_URBAN_FABRIC_ROAD_VALUES.copy())
    major_road_values: list[str] = field(default_factory=lambda: MAJOR_TRANSPORT_ROAD_VALUES.copy())
    growth: GrowthConfig = field(default_factory=GrowthConfig)
    refinement: RefinementConfig = field(default_factory=RefinementConfig)

    def resolved_slug(self) -> str:
        return self.city_slug or city_slug(self.city_name)

    def resolved_metric_crs(self) -> str | None:
        return self.metric_crs or infer_metric_crs_from_city_name(self.city_name)

    def resolved_building_layer(self) -> str:
        return self.building_layer or f"{self.resolved_slug()}_buildings"

    def resolved_road_layer(self) -> str:
        return self.road_layer or f"{self.resolved_slug()}_roads"


def _path_value(value: Any) -> Path:
    return value if isinstance(value, Path) else Path(str(value))


def _growth_from_dict(value: dict[str, Any] | None) -> GrowthConfig:
    return GrowthConfig(**(value or {}))


def _refinement_from_dict(value: dict[str, Any] | None) -> RefinementConfig:
    return RefinementConfig(**(value or {}))


def load_city_config(path: str | Path, city_name: str | None = None) -> CityConfig:
    """Load a city configuration from JSON.

    The file may either be a single config object or a document with a ``cities``
    mapping keyed by city name.
    """
    cfg_path = Path(path)
    payload = json.loads(cfg_path.read_text(encoding="utf-8"))
    if "cities" in payload:
        if city_name is None:
            raise ValueError("city_name is required when config contains a 'cities' mapping")
        try:
            payload = payload["cities"][city_name]
        except KeyError as exc:
            raise KeyError(f"City {city_name!r} is not present in {cfg_path}") from exc
        payload = {"city_name": city_name, **payload}

    data = dict(payload)
    for key in ["data_dir", "boundaries_path", "population_gpkg", "urban_areas_gpkg", "building_gpkg", "road_gpkg"]:
        if key in data:
            data[key] = _path_value(data[key])
    if "growth" in data:
        data["growth"] = _growth_from_dict(data["growth"])
    if "refinement" in data:
        data["refinement"] = _refinement_from_dict(data["refinement"])
    return CityConfig(**data)
