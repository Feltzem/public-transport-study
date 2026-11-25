"""Local OSM feature extraction from prepared city PBF files."""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import geopandas as gpd


ROAD_FILTERS = ("w/highway",)
WATER_FILTERS = (
    "wr/natural=water",
    "wr/natural=bay",
    "wr/natural=strait",
    "wr/natural=coastline",
    "wr/waterway=riverbank",
    "wr/waterway=dock",
    "wr/waterway=canal",
    "wr/landuse=reservoir",
    "wr/landuse=basin",
)


def city_pbf_path(city_slug: str) -> Path:
    return Path("data/osm") / f"{city_slug}.osm.pbf"


def _osmium_executable() -> str:
    executable = shutil.which("osmium")
    if executable is None:
        raise RuntimeError("osmium is required to read local OSM PBF data")
    # Conda currently returns an uppercase .EXE spelling that osmium-tool 1.19
    # misinterprets as a command name on Windows.
    return str(Path(executable).with_name(Path(executable).name.lower()))


def extract_osm_features(
    pbf_path: Path,
    *,
    city_slug: str,
    feature_kind: str,
    filters: tuple[str, ...],
    geometry_types: str,
) -> gpd.GeoDataFrame:
    """Filter a city PBF with osmium and cache a GeoJSON sequence."""
    if not pbf_path.exists() or pbf_path.stat().st_size == 0:
        raise FileNotFoundError(pbf_path)
    cache_dir = Path("cache/osm_features")
    cache_dir.mkdir(parents=True, exist_ok=True)
    filtered = cache_dir / f"{city_slug}_{feature_kind}.osm.pbf"
    exported = cache_dir / f"{city_slug}_{feature_kind}_v2.geojsonseq"
    osmium = _osmium_executable()
    export_config = Path(__file__).with_name("osmium_export_config.json")

    if not filtered.exists() or filtered.stat().st_size == 0:
        temporary = filtered.with_suffix(filtered.suffix + ".part")
        subprocess.run(
            [osmium, "tags-filter", str(pbf_path), *filters, "-f", "pbf", "-o", str(temporary)],
            check=True,
        )
        temporary.replace(filtered)
    if not exported.exists() or exported.stat().st_size == 0:
        temporary = exported.with_suffix(exported.suffix + ".part")
        subprocess.run(
            [
                osmium,
                "export",
                str(filtered),
                "--geometry-types",
                geometry_types,
                "--config",
                str(export_config),
                "--output-format",
                "geojsonseq",
                "--output",
                str(temporary),
            ],
            check=True,
        )
        temporary.replace(exported)

    features = gpd.read_file(exported)
    if features.crs is None:
        features = features.set_crs("EPSG:4326")
    return features


def load_pbf_roads(pbf_path: Path, *, city_slug: str) -> gpd.GeoDataFrame:
    return extract_osm_features(
        pbf_path,
        city_slug=city_slug,
        feature_kind="roads",
        filters=ROAD_FILTERS,
        geometry_types="linestring,polygon",
    )


def load_pbf_water(pbf_path: Path, *, city_slug: str) -> gpd.GeoDataFrame:
    return extract_osm_features(
        pbf_path,
        city_slug=city_slug,
        feature_kind="water",
        filters=WATER_FILTERS,
        geometry_types="linestring,polygon",
    )
