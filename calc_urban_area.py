"""Command-line runner for robust urban-area detection.

This script is the reusable replacement for the heavy exploratory notebook.
The notebook can stay thin: choose a config/city, call this pipeline, and map
the returned layers.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import time

import study_state
from urban_area import CityConfig, load_city_config, run_urban_area_pipeline


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run urban-area detection for one city.")
    parser.add_argument("--city", required=True, help='City name, for example "Stockholm, Sweden".')
    parser.add_argument("--config", type=Path, help="Optional JSON config file.")
    parser.add_argument("--query-missing", action="store_true", help="Query OSM when cached buildings/roads are missing.")
    parser.add_argument("--no-save", action="store_true", help="Run the pipeline without writing output layers.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.config:
        config = load_city_config(args.config, city_name=args.city)
    else:
        config = CityConfig(city_name=args.city)
    started = time.perf_counter()
    result = run_urban_area_pipeline(config, query_missing=args.query_missing, write_outputs=not args.no_save)
    if args.config and not args.no_save:
        study_state.write_state(
            config.resolved_slug(), "urban", study_state.city_payload(args.config, args.city),
            study_state.code_version(), time.perf_counter() - started,
        )
    report = result["validation_report"]
    print(f"City: {config.city_name}")
    print("Area type: morphological built-up urban area")
    print(f"Metric CRS: {result['metric_crs']}")
    print(f"Building mode: {result['building_mode']}")
    print(f"Urban cells: {len(result['urban_cells']):,}")
    print(f"Urban area: {report['summary']['urban_area_sqkm']:.2f} sq km")
    print(f"Urban population share: {report['summary']['urban_population_share']:.1%}")
    if result["outputs"]:
        print("Outputs:")
        for key, value in result["outputs"].items():
            print(f"  {key}: {value}")


if __name__ == "__main__":
    main()
