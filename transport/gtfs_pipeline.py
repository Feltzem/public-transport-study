"""r5py travel-time and accessibility stage.

GTFS frequency now runs inside the transport stage, where stations are built
from the feed; this stage routes door-to-door public transport trips.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from .config import TransportConfig
from .gtfs import download_gtfs_feeds, feed_provenance, prepare_r5_feed, r5_feed_path
from .io import load_transport_inputs, write_json
from .travel_time import compute_travel_time_metrics


def run_gtfs_pipeline(
    config: TransportConfig,
    *,
    service_date: date | None = None,
    force_download: bool = False,
    all_to_all: bool = False,
    write_outputs: bool = True,
) -> dict[str, Any]:
    slug = config.resolved_slug()
    service_date = service_date or config.resolved_service_date()
    if config.osm_pbf is None or config.centre_lon is None or config.centre_lat is None:
        raise ValueError(f"osm_pbf and centre_lon/centre_lat are required for {config.city_name} travel-time routing")
    inputs = load_transport_inputs(config)
    urban_cells = inputs["urban_cells"]
    feeds = download_gtfs_feeds(config, force=force_download)
    min_lon, min_lat, max_lon, max_lat = inputs["boundary"].to_crs("EPSG:4326").total_bounds
    bbox = (min_lon - 0.05, min_lat - 0.05, max_lon + 0.05, max_lat + 0.05)
    routing_feeds = [prepare_r5_feed(path, r5_feed_path(path, slug), bbox=bbox) for path in feeds]
    travel_metrics, centre_matrix, all_cells_matrix = compute_travel_time_metrics(
        urban_cells,
        osm_pbf=config.osm_pbf,
        gtfs_paths=routing_feeds,
        centre_lon=config.centre_lon,
        centre_lat=config.centre_lat,
        service_date=service_date,
        start_hour=config.gtfs_window[0],
        window_hours=config.gtfs_window[1] - config.gtfs_window[0],
        all_to_all=all_to_all,
    )
    summary = {
        "city": config.city_name,
        "city_slug": slug,
        "service_date": service_date.isoformat(),
        "departure_window": f"{config.gtfs_window[0]:02d}:00-{config.gtfs_window[1]:02d}:00",
        "urban_population": float(urban_cells["population"].sum()),
        **travel_metrics,
    }
    outputs: dict[str, str] = {}
    if write_outputs:
        centre_path = config.data_dir / f"travel_time_to_centre_{slug}.parquet"
        centre_matrix.to_parquet(centre_path, index=False)
        outputs["travel_time_to_centre"] = str(centre_path)
        if all_cells_matrix is not None:
            all_path = config.data_dir / f"travel_time_all_cells_{slug}.parquet"
            all_cells_matrix.to_parquet(all_path, index=False)
            outputs["travel_time_all_cells"] = str(all_path)
        provenance_dir = config.data_dir / "provenance"
        write_json(
            provenance_dir / f"{slug}_gtfs_provenance.json",
            {
                "summary": summary,
                "centre": {"lon": config.centre_lon, "lat": config.centre_lat, "label": config.centre_label},
                "feed_paths": [str(path) for path in feeds],
                "gtfs_feed_files": feed_provenance(config, feeds),
                "outputs": outputs,
            },
        )
        write_json(provenance_dir / f"{slug}_gtfs_validation.json", {"summary": summary})
    return {"summary": summary, "outputs": outputs, "to_centre": centre_matrix, "all_cells": all_cells_matrix}
