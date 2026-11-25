"""Download Microsoft Global ML Building Footprints for one configured city.

The previous prototype mixed projected UTM bounds with mercantile tile
coordinates. This script keeps tile discovery and AOI filtering in EPSG:4326,
then calculates footprint area in the city metric CRS.
"""

from __future__ import annotations

import argparse
import gzip
import json
from pathlib import Path
from urllib.request import urlretrieve

import geopandas as gpd
import mercantile
import pandas as pd
from shapely import geometry
from shapely.ops import unary_union
from tqdm import tqdm

from urban_area.config import CityConfig
from urban_area.io import load_city_boundary

DATASET_LINKS_URL = "https://minedbuildings.z5.web.core.windows.net/global-buildings/dataset-links.csv"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download Microsoft building footprints for one city.")
    parser.add_argument("--city", default="Stockholm, Sweden", help='City name, for example "Stockholm, Sweden".')
    parser.add_argument("--zoom", type=int, default=9, help="Microsoft dataset quadkey zoom.")
    parser.add_argument("--output-gpkg", type=Path, default=Path("./data/building_footprints.gpkg"))
    parser.add_argument("--layer", help="Override output layer name.")
    parser.add_argument("--cache-dir", type=Path, default=Path("./cache/ms_buildings"))
    parser.add_argument("--force-download", action="store_true", help="Refresh the links file and requested tiles.")
    return parser.parse_args()


def cached_download(url: str, path: Path, *, force: bool = False) -> Path:
    """Download a source once and reuse it on subsequent city runs."""
    if force or not path.exists() or path.stat().st_size == 0:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".part")
        urlretrieve(url, temporary)
        temporary.replace(path)
    return path


def read_ms_tile(
    url: str,
    cache_path: Path | None = None,
    *,
    force: bool = False,
    clip_geometry=None,
) -> gpd.GeoDataFrame:
    """Read a compressed Microsoft GeoJSONL tile without expanding it in memory.

    Microsoft currently publishes ``.csv.gz`` objects.  The local cache uses a
    stable quadkey-based filename, so compression is detected from the file
    header rather than its extension.  Filtering each feature while streaming
    is important for large tiles whose extent is much wider than a city AOI.
    """

    if cache_path is None:
        raise ValueError("A cache_path is required for streamed Microsoft tiles")
    source = cached_download(url, cache_path, force=force)
    with source.open("rb") as probe:
        compressed = probe.read(2) == b"\x1f\x8b"
    opener = gzip.open if compressed else open
    records = []
    with opener(source, "rt", encoding="utf-8") as handle:
        for line in handle:
            record = json.loads(line)
            raw_geometry = record.get("geometry")
            if not raw_geometry:
                continue
            feature_geometry = geometry.shape(raw_geometry)
            if feature_geometry.is_empty or not feature_geometry.is_valid:
                continue
            if feature_geometry.geom_type not in {"Polygon", "MultiPolygon"}:
                continue
            if clip_geometry is not None and not feature_geometry.intersects(clip_geometry):
                continue
            record["geometry"] = feature_geometry
            records.append(record)
    if not records:
        return gpd.GeoDataFrame(columns=["geometry"], geometry="geometry", crs="EPSG:4326")
    return gpd.GeoDataFrame(records, geometry="geometry", crs="EPSG:4326")


def main() -> None:
    args = parse_args()
    config = CityConfig(city_name=args.city)
    slug = config.resolved_slug()
    layer = args.layer or f"{slug}_buildings"

    city_boundary_metric, metric_crs = load_city_boundary(config)
    aoi_wgs84 = unary_union(city_boundary_metric.to_crs("EPSG:4326").geometry)
    west, south, east, north = aoi_wgs84.bounds
    quad_keys = sorted({mercantile.quadkey(tile) for tile in mercantile.tiles(west, south, east, north, [args.zoom])})
    print(f"{args.city}: {len(quad_keys)} Microsoft tiles at z{args.zoom}")

    links_path = cached_download(
        DATASET_LINKS_URL,
        args.cache_dir / "dataset-links.csv",
        force=args.force_download,
    )
    links = pd.read_csv(links_path, dtype=str)
    frames = []
    for quad_key in tqdm(quad_keys):
        rows = links[links["QuadKey"] == quad_key]
        if rows.empty:
            print(f"QuadKey not found in dataset: {quad_key}")
            continue
        url = rows.iloc[0]["Url"]
        tile_gdf = read_ms_tile(
            url,
            args.cache_dir / "tiles" / f"{quad_key}.jsonl",
            force=args.force_download,
            clip_geometry=aoi_wgs84,
        )
        tile_gdf = tile_gdf[tile_gdf.geometry.notna() & tile_gdf.geometry.is_valid]
        tile_gdf = tile_gdf[tile_gdf.geom_type.isin(["Polygon", "MultiPolygon"])]
        tile_gdf = tile_gdf[tile_gdf.geometry.intersects(aoi_wgs84)].copy()
        if tile_gdf.empty:
            continue
        frames.append(tile_gdf)

    if frames:
        buildings = pd.concat(frames, ignore_index=True)
        buildings = gpd.GeoDataFrame(buildings, geometry="geometry", crs="EPSG:4326")
        buildings["_geom_key"] = buildings.geometry.apply(lambda geom: geom.wkb_hex)
        buildings = buildings.drop_duplicates("_geom_key").drop(columns=["_geom_key"])
        buildings_metric = buildings.to_crs(metric_crs)
        buildings["area_sqm"] = buildings_metric.geometry.area
    else:
        buildings = gpd.GeoDataFrame(columns=["geometry", "area_sqm"], geometry="geometry", crs="EPSG:4326")

    args.output_gpkg.parent.mkdir(parents=True, exist_ok=True)
    buildings.to_file(args.output_gpkg, layer=layer, driver="GPKG")
    print(f"Saved {len(buildings):,} footprint polygons to {args.output_gpkg}/{layer}")


if __name__ == "__main__":
    main()
