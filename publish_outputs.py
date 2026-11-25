"""Publish harmonised six-city layers atomically in EPSG:3035."""

from __future__ import annotations

import argparse
from datetime import date
import json
import os
from pathlib import Path

import fiona
import geopandas as gpd
import pandas as pd

from transport.config import load_transport_config
from transport.io import list_layers


TARGET_CRS = "EPSG:3035"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Publish ArcGIS-facing study outputs.")
    parser.add_argument("--config", type=Path, default=Path("city_config.example.json"))
    parser.add_argument("--output", type=Path, default=Path("../Output/pt_study.gpkg"))
    parser.add_argument("--allow-incomplete", action="store_true")
    return parser.parse_args()


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _layer(path: Path, name: str, *, required: bool) -> gpd.GeoDataFrame:
    if name not in list_layers(path):
        if required:
            raise FileNotFoundError(f"Required layer {path}/{name} is missing")
        return gpd.GeoDataFrame(columns=["geometry"], geometry="geometry", crs=TARGET_CRS)
    result = gpd.read_file(path, layer=name)
    return result.to_crs(TARGET_CRS)


def _select(frame: gpd.GeoDataFrame, mapping: dict[str, str | tuple[str, object]]) -> gpd.GeoDataFrame:
    output = gpd.GeoDataFrame(geometry=frame.geometry, crs=frame.crs)
    for target, source in mapping.items():
        if isinstance(source, tuple):
            source_name, default = source
        else:
            source_name, default = source, None
        output[target] = frame[source_name] if source_name in frame else default
    return output


def _write_attributes(path: Path, layer: str, table: pd.DataFrame) -> None:
    def field_type(series: pd.Series) -> str:
        if pd.api.types.is_integer_dtype(series):
            return "int"
        if pd.api.types.is_numeric_dtype(series):
            return "float"
        return "str"

    schema = {"geometry": "None", "properties": {column: field_type(table[column]) for column in table.columns}}
    with fiona.open(path, "w", driver="GPKG", layer=layer, schema=schema) as sink:
        for row in table.where(pd.notna(table), None).to_dict(orient="records"):
            clean = {key: value.item() if hasattr(value, "item") else value for key, value in row.items()}
            sink.write({"geometry": None, "properties": clean})


def _same_population(a, b) -> bool:
    if a is None or b is None:
        return False
    return abs(float(a) - float(b)) <= 1e-6 * max(1.0, abs(float(a)))


def _check_consistency(city_name, urban_summary, transport_summary, gtfs_summary, *, allow_incomplete: bool) -> dict[str, bool]:
    """Ensure downstream metrics were computed on the current urban area.

    Raises on a mismatch; with ``allow_incomplete`` the stale metrics are
    blanked instead of being published next to a different urban area.
    """
    urban_pop = urban_summary.get("urban_population")
    current = {}
    for stage, summary in [("transport", transport_summary), ("gtfs", gtfs_summary)]:
        stage_pop = summary.get("urban_population")
        if stage_pop is None:
            current[stage] = bool(summary)
            continue
        current[stage] = _same_population(urban_pop, stage_pop)
        if not current[stage]:
            message = (
                f"{city_name}: {stage} metrics were computed on an urban population of {stage_pop:,.0f}, "
                f"but the current urban area has {urban_pop:,.0f}; re-run the {stage} stage"
            )
            if not allow_incomplete:
                raise RuntimeError(message)
            print(f"WARNING: {message}. Omitting stale {stage} metrics.")
    return current


def publish(config_path: Path, output_path: Path, *, allow_incomplete: bool = False) -> dict[str, int]:
    document = _read_json(config_path)
    frames: dict[str, list[gpd.GeoDataFrame]] = {
        "urban_boundary": [],
        "urban_cells": [],
        "rail_lines": [],
        "rail_stations": [],
        "isochrones_15min": [],
        "coverage_cells_15min": [],
        "pt_stops_all_modes": [],
        "catchments_all_modes": [],
    }
    summaries = []
    required = not allow_incomplete
    for city_name, raw in document["cities"].items():
        config = load_transport_config(config_path, city_name)
        slug = config.resolved_slug()
        provenance_dir = config.data_dir / "provenance"
        urban_validation = _read_json(provenance_dir / f"{slug}_growing_refined_validation.json")
        urban_provenance = _read_json(provenance_dir / f"{slug}_growing_refined_provenance.json")
        # Runs before the DEGURBA revision carry no urban_detection block.
        urban_method = urban_provenance.get("urban_detection", {}).get("method", "score_legacy")
        transport_validation = _read_json(provenance_dir / f"{slug}_transport_validation.json")
        gtfs_provenance = _read_json(provenance_dir / f"{slug}_gtfs_provenance.json")

        boundary = _layer(config.urban_areas_gpkg, f"urban_area_{slug}_growing_refined", required=required)
        if not boundary.empty:
            boundary_out = _select(
                boundary,
                {
                    "city": ("city_name", city_name),
                    "city_slug": ("city_slug", slug),
                    "method": ("method", "growing_refined"),
                    "area_km2": ("area_sqkm", None),
                    "urban_pop": ("total_population", None),
                    "run_date": ("run_date", date.today().isoformat()),
                },
            )
            boundary_out["area_km2"] = boundary_out.geometry.area / 1_000_000
            frames["urban_boundary"].append(boundary_out)

        urban_cells = _layer(config.urban_areas_gpkg, f"urban_cells_{slug}_growing_refined", required=required)
        if not urban_cells.empty:
            urban_out = _select(
                urban_cells,
                {
                    "city": ("Name", city_name),
                    "city_slug": ("city_slug", slug),
                    "degurba_class": ("degurba_class", None),
                    "pop": ("population", 0.0),
                    "pop_density": ("population_density", None),
                    "built_up_share": ("built_up_coverage", None),
                    "building_area": ("building_area_sqm", 0.0),
                    "in_urban_area": ("in_urban_area", True),
                },
            )
            frames["urban_cells"].append(urban_out)

        rail_lines = _layer(config.rail_gpkg, f"rail_lines_{slug}", required=required)
        if not rail_lines.empty:
            frames["rail_lines"].append(
                _select(rail_lines, {"city": ("city", city_name), "city_slug": ("city_slug", slug), "mode": ("mode", "rail"), "name": ("name", None)})
            )
        stations = _layer(config.rail_gpkg, f"rail_stations_{slug}", required=required)
        if not stations.empty:
            frames["rail_stations"].append(
                _select(
                    stations,
                    {
                        "city": ("city", city_name),
                        "city_slug": ("city_slug", slug),
                        "mode": ("mode", "rail"),
                        "name": ("name", None),
                        "departures_per_hr": ("departures_per_hr", None),
                        "departures_per_hr_total": ("departures_per_hr_total", None),
                        "urban_catchment_pop": ("urban_catchment_population", None),
                    },
                )
            )
        isochrones = _layer(config.isochrone_gpkg, f"isochrones_15min_{slug}", required=required)
        if not isochrones.empty:
            frames["isochrones_15min"].append(
                _select(isochrones, {"city": ("city", city_name), "city_slug": ("city_slug", slug), "run_date": ("run_date", date.today().isoformat())})
            )
        coverage = _layer(config.isochrone_gpkg, f"coverage_cells_15min_{slug}", required=required)
        if not coverage.empty:
            frames["coverage_cells_15min"].append(
                _select(
                    coverage,
                    {
                        "city": ("city", city_name),
                        "city_slug": ("city_slug", slug),
                        "pop": ("population", 0.0),
                        "covered_pop": ("covered_pop", 0.0),
                        "covered_frac": ("covered_frac", 0.0),
                    },
                )
            )

        stops = _layer(config.rail_gpkg, f"pt_stops_all_modes_{slug}", required=False)
        if not stops.empty:
            frames["pt_stops_all_modes"].append(
                _select(stops, {"city": ("city", city_name), "city_slug": ("city_slug", slug), "mode": ("mode", None), "name": ("name", None), "departures_per_hr": ("departures_per_hr", None)})
            )
        tiers = _layer(config.isochrone_gpkg, f"catchments_all_modes_{slug}", required=False)
        if not tiers.empty:
            frames["catchments_all_modes"].append(
                _select(tiers, {"city": ("city", city_name), "city_slug": ("city_slug", slug), "tier": ("tier", None)})
            )

        urban_summary = urban_validation.get("summary", {})
        transport_summary = transport_validation.get("coverage", {})
        frequency = transport_validation.get("frequency", {})
        all_modes = transport_validation.get("all_modes", {})
        gtfs_summary = gtfs_provenance.get("summary", {})
        transport_current = _check_consistency(
            city_name, urban_summary, transport_summary, gtfs_summary, allow_incomplete=allow_incomplete
        )
        if not transport_current["transport"]:
            transport_summary, frequency, all_modes, transport_validation = {}, {}, {}, {}
        if not transport_current["gtfs"]:
            gtfs_summary = {}
        summaries.append(
            {
                "city": city_name,
                "city_slug": slug,
                "urban_method": urban_method,
                "urban_area_km2": urban_summary.get("urban_area_sqkm"),
                "urban_pop": urban_summary.get("urban_population"),
                "pct_urban": 100 * urban_summary["urban_population_share"] if urban_summary.get("urban_population_share") is not None else None,
                "pct_within_15min": transport_summary.get("pct_within_15min"),
                "pct_within_15min_frequent": frequency.get("pct_within_15min_frequent"),
                "pct_any_stop_500m": all_modes.get("pct_any_stop"),
                "pct_frequent_any_mode": all_modes.get("pct_frequent_any_mode"),
                "n_rail_stations": transport_validation.get("kept_station_count"),
                "n_rail_stations_frequent": transport_validation.get("frequent_station_count"),
                "n_stops_all_modes": all_modes.get("n_stops_all_modes"),
                "dep_per_hr_median": transport_validation.get("dep_per_hr_median"),
                "service_date": frequency.get("service_date"),
                "median_tt_centre_min": gtfs_summary.get("median_tt_centre_min"),
                "pct_centre_30min": gtfs_summary.get("pct_centre_30min"),
                "access_pop_30min_pct": gtfs_summary.get("access_pop_30min_pct"),
                "access_pop_45min_pct": gtfs_summary.get("access_pop_45min_pct"),
            }
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(f"{output_path.stem}.tmp{output_path.suffix}")
    if temporary.exists():
        temporary.unlink()
    counts = {}
    for layer_name, parts in frames.items():
        if not parts:
            if required:
                raise RuntimeError(f"No features available for required published layer {layer_name}")
            continue
        combined = gpd.GeoDataFrame(pd.concat(parts, ignore_index=True), geometry="geometry", crs=TARGET_CRS)
        combined.to_file(temporary, layer=layer_name, driver="GPKG", mode="w")
        counts[layer_name] = int(len(combined))
    summary_table = pd.DataFrame(summaries)
    _write_attributes(temporary, "city_summary", summary_table)
    counts["city_summary"] = int(len(summary_table))
    os.replace(temporary, output_path)
    summary_table.to_csv(output_path.with_name("city_summary.csv"), index=False)
    return counts


def main() -> None:
    args = parse_args()
    counts = publish(args.config, args.output, allow_incomplete=args.allow_incomplete)
    print(f"Published {args.output} in {TARGET_CRS}")
    for layer, count in counts.items():
        print(f"{layer}: {count:,}")


if __name__ == "__main__":
    main()
