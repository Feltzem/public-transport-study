"""Coverage sensitivity to the buffer width around reachable walking streets.

Run after calc_transport.py, from Code/:
    python catchment_sensitivity.py --city "Helsinki, Finland"
Writes data/provenance/{slug}_catchment_sensitivity.json and prints each row.
Dissolved multi-source catchments are equivalent to dissolving the
per-station catchments, so the 25 m row should match the transport stage.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import time

import geopandas as gpd

from transport import load_transport_config
from transport.coverage import polygon_coverage
from transport.io import load_transport_inputs, write_json
from transport.isochrones import build_combined_isochrone
from transport.network import CsrGraph, load_or_download_walk_graph


def main() -> None:
    parser = argparse.ArgumentParser(description="Catchment buffer-width sensitivity for one city.")
    parser.add_argument("--city", required=True)
    parser.add_argument("--config", type=Path, default=Path("city_config.example.json"))
    parser.add_argument("--buffers", default="25,50,100", help="Comma-separated buffer widths in metres.")
    args = parser.parse_args()

    config = load_transport_config(args.config, args.city)
    crs = config.resolved_metric_crs()
    slug = config.resolved_slug()
    inputs = load_transport_inputs(config)
    graph = load_or_download_walk_graph(inputs["boundary"], cache_path=config.walk_graph_path(), metric_crs=crs)
    csr = CsrGraph(graph)
    stations = gpd.read_file(config.rail_gpkg, layer=f"rail_stations_{slug}").to_crs(crs)
    stops = gpd.read_file(config.rail_gpkg, layer=f"pt_stops_all_modes_{slug}").to_crs(crs)
    tiers = [
        ("rail_15min", stations, config.walking_distance_m),
        ("any_stop", stops, config.bus_walking_distance_m),
    ]
    rows = []
    for buffer_m in [float(value) for value in args.buffers.split(",")]:
        for tier, points, cutoff in tiers:
            started = time.perf_counter()
            polygon = build_combined_isochrone(graph, points, cutoff_m=cutoff, buffer_m=buffer_m, csr=csr)
            summary, _ = polygon_coverage(inputs["urban_cells"], polygon, buildings=inputs["buildings"])
            rows.append(
                {
                    "buffer_m": buffer_m,
                    "tier": tier,
                    "walk_distance_m": cutoff,
                    "pct_covered": summary["pct_within_15min"],
                    "seconds": round(time.perf_counter() - started, 1),
                }
            )
            print(rows[-1], flush=True)
    write_json(
        config.data_dir / "provenance" / f"{slug}_catchment_sensitivity.json",
        {"city": config.city_name, "urban_population": float(inputs["urban_cells"]["population"].sum()), "rows": rows},
    )


if __name__ == "__main__":
    main()
