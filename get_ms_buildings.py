"""Download Microsoft Global ML Building Footprints for one configured city.

The previous prototype mixed projected UTM bounds with mercantile tile
coordinates. This script keeps tile discovery and AOI filtering in EPSG:4326,
then calculates footprint area in the city metric CRS.
"""

from __future__ import annotations

import argparse
from pathlib import Path

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
    return parser.parse_args()


def read_ms_tile(url: str) -> gpd.GeoDataFrame:
    df = pd.read_json(url, lines=True)
    df["geometry"] = df["geometry"].apply(geometry.shape)
    return gpd.GeoDataFrame(df, geometry="geometry", crs="EPSG:4326")


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

    links = pd.read_csv(DATASET_LINKS_URL, dtype=str)
    frames = []
    for quad_key in tqdm(quad_keys):
        rows = links[links["QuadKey"] == quad_key]
        if rows.empty:
            print(f"QuadKey not found in dataset: {quad_key}")
            continue
        url = rows.iloc[0]["Url"]
        tile_gdf = read_ms_tile(url)
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
