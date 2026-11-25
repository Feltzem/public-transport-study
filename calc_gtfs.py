"""CLI for r5py travel-time accessibility (GTFS frequency runs in calc_transport.py)."""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path
import time

import study_state
from transport.config import load_transport_config
from transport.gtfs_pipeline import run_gtfs_pipeline


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Calculate r5py travel-time accessibility.")
    parser.add_argument("--city", required=True)
    parser.add_argument("--config", type=Path, default=Path("city_config.example.json"))
    parser.add_argument("--date", type=date.fromisoformat, help="Override the configured service date (YYYY-MM-DD).")
    parser.add_argument("--force-download", action="store_true")
    parser.add_argument("--all-to-all", action="store_true")
    parser.add_argument("--no-save", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_transport_config(args.config, args.city)
    started = time.perf_counter()
    result = run_gtfs_pipeline(
        config,
        service_date=args.date,
        force_download=args.force_download,
        all_to_all=args.all_to_all,
        write_outputs=not args.no_save,
    )
    if not args.no_save and args.date is None:
        study_state.write_state(
            config.resolved_slug(), "gtfs", study_state.city_payload(args.config, args.city),
            study_state.code_version(), time.perf_counter() - started,
        )
    for key, value in result["summary"].items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    main()
