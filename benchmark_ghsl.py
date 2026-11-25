"""Compare study urban boundaries/population with a GHSL urban-centre layer."""

from __future__ import annotations

import argparse
from pathlib import Path

import geopandas as gpd
import pandas as pd


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark published city results against GHSL.")
    parser.add_argument("--study", type=Path, default=Path("../Output/pt_study.gpkg"))
    parser.add_argument("--ghsl", type=Path, required=True)
    parser.add_argument("--ghsl-layer", required=True)
    parser.add_argument("--ghsl-pop-field", default="P15")
    parser.add_argument("--ghsl-area-field", help="Existing GHSL area-km2 field; geometry area is used if omitted.")
    parser.add_argument("--output", type=Path, default=Path("../Output/ghsl_benchmark.csv"))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    study = gpd.read_file(args.study, layer="urban_boundary").to_crs("EPSG:3035")
    ghsl = gpd.read_file(args.ghsl, layer=args.ghsl_layer).to_crs("EPSG:3035")
    study_points = study.copy()
    study_points.geometry = study_points.geometry.centroid
    nearest = gpd.sjoin_nearest(
        study_points[["city", "city_slug", "area_km2", "urban_pop", "geometry"]],
        ghsl,
        how="left",
        distance_col="centroid_distance_m",
    )
    nearest["ghsl_area_km2"] = (
        nearest[args.ghsl_area_field]
        if args.ghsl_area_field
        else nearest["index_right"].map(ghsl.geometry.area / 1_000_000)
    )
    nearest["ghsl_population"] = nearest[args.ghsl_pop_field]
    nearest["area_ratio_study_to_ghsl"] = nearest["area_km2"] / nearest["ghsl_area_km2"]
    nearest["population_ratio_study_to_ghsl"] = nearest["urban_pop"] / nearest["ghsl_population"]
    columns = [
        "city",
        "city_slug",
        "area_km2",
        "urban_pop",
        "ghsl_area_km2",
        "ghsl_population",
        "area_ratio_study_to_ghsl",
        "population_ratio_study_to_ghsl",
        "centroid_distance_m",
    ]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(nearest[columns]).to_csv(args.output, index=False)
    print(f"Wrote {args.output}")


if __name__ == "__main__":
    main()
