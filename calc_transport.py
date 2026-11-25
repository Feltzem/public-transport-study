"""CLI for GTFS stations, walking-network catchments, frequency, and coverage."""

from __future__ import annotations

import argparse
from pathlib import Path
import time

import study_state
from transport import load_transport_config, run_transport_pipeline


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Calculate rail-station walking catchments for one city.")
    parser.add_argument("--city", required=True)
    parser.add_argument("--config", type=Path, default=Path("city_config.example.json"))
    parser.add_argument("--force-network", action="store_true")
    parser.add_argument("--force-osm", action="store_true")
    parser.add_argument("--force-gtfs", action="store_true", help="Re-download the GTFS feeds.")
    parser.add_argument("--no-save", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_transport_config(args.config, args.city)
    started = time.perf_counter()
    result = run_transport_pipeline(
        config,
        force_network=args.force_network,
        force_osm=args.force_osm,
        force_gtfs=args.force_gtfs,
        write_outputs=not args.no_save,
    )
    if not args.no_save:
        study_state.write_state(
            config.resolved_slug(), "transport", study_state.city_payload(args.config, args.city),
            study_state.code_version(), time.perf_counter() - started,
        )
    summary = result["summary"]
    print(f"City: {config.city_name}")
    print(f"Kept stations: {len(result['stations']):,}")
    print(f"Weighted population within 15 minutes of rail: {summary['pct_within_15min']:.2f}%")
    print(f"...of frequent rail: {result['frequency']['pct_within_15min_frequent']:.2f}%")
    print(f"Population within {config.bus_walking_distance_m:.0f} m of any stop: {result['all_modes']['pct_any_stop']:.2f}%")
    print(f"Population with frequent service of any mode: {result['all_modes']['pct_frequent_any_mode']:.2f}%")
    print(f"Legacy centroid estimate on this run: {summary['legacy_centroid_pct']:.2f}%")
    for name, path in result["outputs"].items():
        print(f"{name}: {path}")


if __name__ == "__main__":
    main()
