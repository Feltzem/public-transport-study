"""Calculate road density per population cell using the shared metric-CRS pipeline."""

from __future__ import annotations

import argparse
from pathlib import Path

from urban_area.config import CityConfig
from urban_area.io import load_city_boundary, load_population_cells, load_roads
from urban_area.roads import aggregate_roads_to_cells, normalize_roads


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Aggregate road density to population cells.")
    parser.add_argument("--city", default="Oslo, Norway", help='City name, for example "Oslo, Norway".')
    parser.add_argument("--population-gpkg", type=Path, default=Path("./data/population_density.gpkg"))
    parser.add_argument("--output-gpkg", type=Path, default=Path("./data/population_density.gpkg"))
    parser.add_argument("--output-layer", help="Override output layer name.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = CityConfig(city_name=args.city, population_gpkg=args.population_gpkg)
    slug = config.resolved_slug()
    output_layer = args.output_layer or f"population_density_with_roads_{slug}"

    city_boundary, metric_crs = load_city_boundary(config)
    cells = load_population_cells(config, city_boundary, metric_crs)
    roads = load_roads(config)
    roads_metric = normalize_roads(roads, metric_crs)
    result = aggregate_roads_to_cells(roads_metric, cells)

    source_km = roads_metric["length_m"].sum() / 1000
    aggregated_km = result["total_road_length_m"].sum() / 1000
    print(f"City: {args.city}")
    print(f"Metric CRS: {metric_crs}")
    print(f"Population cells: {len(result):,}")
    print(f"Source road length: {source_km:.2f} km")
    print(f"Aggregated road length: {aggregated_km:.2f} km")
    print(f"Average road density: {result['road_density_km_per_sqkm'].mean():.2f} km/sq km")

    result.to_file(args.output_gpkg, layer=output_layer, driver="GPKG")
    print(f"Saved {args.output_gpkg}/{output_layer}")


if __name__ == "__main__":
    main()
