"""Resumable multi-city runner for every analytical stage."""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
import subprocess
import sys
import time

import fiona
import pandas as pd

from transport import load_transport_config, run_transport_pipeline
from transport.gtfs_pipeline import run_gtfs_pipeline
from urban_area import load_city_config, run_urban_area_pipeline
import study_state


STUDY_ORDER = ["stockholm", "copenhagen", "helsinki", "brussels", "paris", "oslo"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the six-city public-transport study.")
    parser.add_argument("--cities", default="all", help="all or a comma-separated list of city slugs")
    parser.add_argument("--stage", choices=["urban", "transport", "gtfs", "all"], default="all")
    parser.add_argument("--config", type=Path, default=Path("city_config.example.json"))
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--skip-all-to-all", action="store_true", help="Skip the very large cell-to-cell r5py matrix.")
    return parser.parse_args()


def _payload(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _ensure_buildings(city_name: str, config, *, force: bool) -> None:
    layer = config.resolved_building_layer()
    available = list(fiona.listlayers(config.building_gpkg)) if config.building_gpkg.exists() else []
    if layer in available and not force:
        with fiona.open(config.building_gpkg, layer=layer) as source:
            geometry_type = source.schema.get("geometry", "Unknown")
        point_only = geometry_type in {"Point", "MultiPoint", "3D Point", "3D MultiPoint"}
        footprint_required = config.building_mode in {"footprint", "required_footprint"}
        if not (point_only and footprint_required):
            return
    command = [
        sys.executable,
        str(Path(__file__).with_name("get_ms_buildings.py")),
        "--city",
        city_name,
        "--output-gpkg",
        str(config.building_gpkg),
        "--layer",
        layer,
    ]
    if force:
        command.append("--force-download")
    subprocess.run(command, check=True)


def main() -> None:
    args = parse_args()
    document = _payload(args.config)
    by_slug = {settings.get("city_slug"): name for name, settings in document["cities"].items()}
    requested = STUDY_ORDER if args.cities == "all" else [item.strip() for item in args.cities.split(",") if item.strip()]
    unknown = sorted(set(requested) - set(by_slug))
    if unknown:
        raise ValueError(f"Unknown city slugs: {unknown}")
    stages = ["urban", "transport", "gtfs"] if args.stage == "all" else [args.stage]
    code_version = study_state.code_version()
    log_dir = Path("data/logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    failures = []

    for slug in requested:
        city_name = by_slug[slug]
        city_payload = document["cities"][city_name]
        logger = logging.getLogger(f"study.{slug}")
        logger.handlers.clear()
        logger.setLevel(logging.INFO)
        handler = logging.FileHandler(log_dir / f"{slug}.log", encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
        for stage in stages:
            # Evaluated per stage, after any upstream stage in this loop has
            # rewritten its outputs, so downstream stages see the new fingerprint.
            if not args.force and study_state.is_current(slug, stage, city_payload, code_version):
                logger.info("Skipping current stage %s", stage)
                rows.append({"city_slug": slug, "stage": stage, "status": "skipped", "elapsed_seconds": 0.0})
                continue
            started = time.perf_counter()
            try:
                logger.info("Starting stage %s", stage)
                if stage == "urban":
                    urban_config = load_city_config(args.config, city_name=city_name)
                    _ensure_buildings(city_name, urban_config, force=args.force)
                    run_urban_area_pipeline(urban_config, query_missing=True, write_outputs=True)
                elif stage == "transport":
                    transport_config = load_transport_config(args.config, city_name)
                    run_transport_pipeline(
                        transport_config,
                        force_network=args.force,
                        force_osm=args.force,
                        force_gtfs=args.force,
                        write_outputs=True,
                    )
                else:
                    transport_config = load_transport_config(args.config, city_name)
                    run_gtfs_pipeline(
                        transport_config,
                        force_download=False,
                        all_to_all=not args.skip_all_to_all,
                        write_outputs=True,
                    )
                elapsed = time.perf_counter() - started
                study_state.write_state(slug, stage, city_payload, code_version, elapsed)
                logger.info("Completed stage %s in %.1f seconds", stage, elapsed)
                rows.append({"city_slug": slug, "stage": stage, "status": "complete", "elapsed_seconds": elapsed})
            except Exception as exc:
                elapsed = time.perf_counter() - started
                logger.exception("Stage %s failed", stage)
                rows.append({"city_slug": slug, "stage": stage, "status": "failed", "elapsed_seconds": elapsed})
                failures.append(f"{slug}/{stage}: {exc}")
                break

    summary = pd.DataFrame(rows)
    summary_path = Path("data/logs/timing_summary.csv")
    summary.to_csv(summary_path, index=False)
    print(summary.to_string(index=False))
    print(f"Timing summary: {summary_path}")
    if failures:
        raise SystemExit("Study stages failed:\n" + "\n".join(failures))


if __name__ == "__main__":
    main()
