"""Render one city's report figures from the working outputs.

Run from Code/: python make_report_figures.py --city "Helsinki, Finland"
Figures are written to ../Report/Images as {slug}_*.pdf plus PNG previews.

Every figure uses the flat theme in report_theme.py and is drawn at the LaTeX
text width, so it is included at 100% and its type matches the page. All maps
of a city share one frame (the urban area at the loosest sensitivity threshold,
plus a margin), so they can be compared figure to figure.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely.ops import unary_union

import report_theme as theme
from report_theme import ACCESS, ACCESS_3, ACCESS_5, CLAY, INK, INK_2, MUTED, PAPER, RAIL_LINE, STAGE
from transport.config import load_transport_config
from transport.io import list_layers
from urban_area import load_city_config

theme.apply()

SENSITIVITY_THRESHOLDS = (1200, 1500, 2000)

# Place names drawn on the maps, as (name, lon, lat). Water names are italic.
# Only names that the report text refers to; add a block per city.
MAP_LABELS = {
    "oslo": [
        {"name": "Asker", "lonlat": (10.4392, 59.8331)},
        {"name": "Sandvika", "lonlat": (10.5236, 59.8910)},
        {"name": "Lysaker", "lonlat": (10.6355, 59.9125)},
        {"name": "Groruddalen", "lonlat": (10.8806, 59.9614)},
        {"name": "Lørenskog", "lonlat": (10.9570, 59.9282)},
        {"name": "Lillestrøm", "lonlat": (11.0492, 59.9560)},
        {"name": "Holmlia", "lonlat": (10.7970, 59.8355)},
        {"name": "Kolbotn", "lonlat": (10.8003, 59.8112)},
        {"name": "Oslofjorden", "lonlat": (10.585, 59.845), "kind": "water"},
        {"name": "Bunnefjorden", "lonlat": (10.726, 59.795), "kind": "water", "rotation": 80},
    ],
}
CENTRE_SHORT_LABEL = {"oslo": "Oslo S"}


# --------------------------------------------------------------------------
# Shared map pieces
# --------------------------------------------------------------------------
def _map_base(ax, data, *, stage: bool = False):
    theme.base_map(ax, data["extent"], data["water"], data["envelope"])
    if stage:
        data["hull"].plot(ax=ax, color=STAGE, linewidth=0, zorder=0.8)


def _finish_map(ax, data, *, labels: bool = True, centre: bool = True, scale: bool = True):
    if labels:
        theme.place_labels(ax, data["labels"], data["extent"])
    if centre:
        theme.centre_marker(ax, data["centre"], data["centre_label"])
    if scale:
        theme.scale_bar(ax, data["extent"])


def _study_outline(ax, data, width: float = 0.9, colour: str = INK, zorder: float = 6):
    data["hull_display"].boundary.plot(ax=ax, color=colour, linewidth=width, zorder=zorder)


def _generalise(geom, min_hole_m2: float = 100_000, min_part_m2: float = 20_000):
    """Drop pinholes and specks from an outline for display only (areas unchanged)."""
    from shapely.geometry import MultiPolygon, Polygon

    parts = getattr(geom, "geoms", [geom])
    kept = []
    for part in parts:
        if part.area < min_part_m2:
            continue
        holes = [ring for ring in part.interiors if Polygon(ring).area >= min_hole_m2]
        kept.append(Polygon(part.exterior, holes))
    return MultiPolygon(kept) if len(kept) > 1 else kept[0]


def _fit_frame(core_bounds, envelope, pad: float = 1200, min_pad: float = 250, step: float = 100):
    """Pad the data bounds, then pull in any side that runs outside the study envelope.

    Keeps the base map free of slivers beyond the envelope, where land and water
    are not mapped. Falls back to the padded frame when the envelope cannot cover it.
    """
    from shapely.geometry import box

    minx, miny, maxx, maxy = core_bounds
    pads = [pad, pad, pad, pad]  # left, bottom, right, top
    shape = envelope.union_all()
    for _ in range(200):
        frame = box(minx - pads[0], miny - pads[1], maxx + pads[2], maxy + pads[3])
        outside = frame.difference(shape)
        if outside.area < 1e4:
            break
        fx0, fy0, fx1, fy1 = frame.bounds
        strips = [box(fx0, fy0, fx0 + step, fy1), box(fx0, fy0, fx1, fy0 + step),
                  box(fx1 - step, fy0, fx1, fy1), box(fx0, fy1 - step, fx1, fy1)]
        order = sorted(range(4), key=lambda i: -strips[i].intersection(outside).area)
        movable = [i for i in order if pads[i] - step >= min_pad and strips[i].intersection(outside).area > 0]
        if not movable:
            break
        pads[movable[0]] -= step
    return (minx - pads[0], miny - pads[1], maxx + pads[2], maxy + pads[3])


# --------------------------------------------------------------------------
# Figures
# --------------------------------------------------------------------------
def figure_degurba(data, out: Path):
    cells = data["analysis"]
    fig, (ax,), key = theme.map_figure(data["extent"])
    _map_base(ax, data)
    colours = {"urban_cluster": CLAY[200], "urban_centre": CLAY[400]}
    for name, colour in colours.items():
        part = cells[cells["degurba_class"] == name]
        if not part.empty:
            part.plot(ax=ax, color=colour, edgecolor=PAPER, linewidth=0.12, zorder=1, rasterized=True)
    if data["water"] is not None:  # water over the class fills: cells are not clipped to land
        data["water"].plot(ax=ax, color=theme.WATER, linewidth=0, zorder=2, rasterized=True)
    _study_outline(ax, data, width=1.0)
    _finish_map(ax, data)
    s = data["class_summary"]
    theme.key_group(key, 0.0, "Degree of Urbanisation", [
        theme.swatch(CLAY[400], f"Urban centre · ≥1,500/km² · {s['urban_centre'] / 1e3:,.0f}k residents"),
        theme.swatch(CLAY[200], f"Urban cluster · ≥300/km² · {s['urban_cluster'] / 1e3:,.0f}k residents"),
        theme.swatch(theme.LAND, f"Rural · {s['rural'] / 1e3:,.0f}k residents", edge=theme.HAIRLINE),
    ])
    theme.key_group(key, 0.56, "Study area", [
        theme.outline_handle(f"Study urban area after edge refinement · {data['urban_pop'] / 1e3:,.0f}k", width=1.0),
        theme.point_handle(f"City centre ({data['centre_label']})", colour=PAPER, edge=INK, size=5),
    ])
    theme.credit(fig, theme.CREDIT_URBAN)
    theme.save(fig, out / f"{data['slug']}_degurba.pdf")


def figure_sensitivity(data, out: Path):
    fig, (ax,), key = theme.map_figure(data["extent"])
    _map_base(ax, data)
    fills = {1200: CLAY[200], 1500: CLAY[400], 2000: CLAY[600]}
    for threshold in SENSITIVITY_THRESHOLDS:  # loosest first so stricter (smaller) areas sit on top
        outline = data["threshold_areas"][threshold]["geometry"]
        gpd.GeoSeries([outline], crs=data["crs"]).plot(ax=ax, color=fills[threshold], linewidth=0, zorder=1 + threshold / 1e4)
    gpd.GeoSeries([_generalise(data["threshold_areas"][1500]["geometry"])], crs=data["crs"]).boundary.plot(
        ax=ax, color=INK, linewidth=0.7, zorder=1.9)  # under the water layer: traces the land edge only
    if data["water"] is not None:
        data["water"].plot(ax=ax, color=theme.WATER, linewidth=0, zorder=2, rasterized=True)
    _finish_map(ax, data)
    handles = []
    for threshold in SENSITIVITY_THRESHOLDS:
        row = data["threshold_areas"][threshold]
        study = " (study threshold)" if threshold == 1500 else ""
        handles.append(theme.swatch(fills[threshold], f"{threshold:,}/km²{study} · {row['area_km2']:,.0f} km² · {row['population'] / 1e3:,.0f}k residents"))
    theme.key_group(key, 0.0, "Urban centre at each density threshold, before edge refinement", handles)
    theme.key_group(key, 0.56, "Study threshold", [theme.outline_handle("Urban centre at 1,500/km\u00b2", width=0.7)])
    key.text(0.56, 0.38, "Stricter thresholds nest inside looser ones: the darkest\nclay is urban at every threshold, the palest only at 1,200.",
             transform=key.transAxes, ha="left", va="top", fontsize=6.4, color=INK_2, linespacing=1.35)
    theme.credit(fig, theme.CREDIT_URBAN)
    theme.save(fig, out / f"{data['slug']}_sensitivity.pdf")
    return pd.DataFrame([{"threshold": t, **{k: v for k, v in data["threshold_areas"][t].items() if k != "geometry"}}
                         for t in SENSITIVITY_THRESHOLDS])


def figure_rail(data, out: Path):
    stations = data["stations"]
    fig, (ax,), key = theme.map_figure(data["extent"])
    _map_base(ax, data, stage=True)
    data["isochrones_union"].plot(ax=ax, color=ACCESS[100], linewidth=0, zorder=1, rasterized=True)
    data["water"].plot(ax=ax, color=theme.WATER, linewidth=0, zorder=2, rasterized=True)
    _study_outline(ax, data, width=0.5, colour=INK_2, zorder=2.5)
    lines = data["rail_lines"]
    widths = {"rail": 0.75, "subway": 0.6, "tram": 0.4}
    for mode, width in widths.items():
        part = lines[lines["mode"] == mode]
        if not part.empty:
            part.plot(ax=ax, color=RAIL_LINE, linewidth=width, zorder=3)
    bands = [(0, 6, ACCESS_3[0], "Fewer than 6"), (6, 12, ACCESS_3[1], "6 to 11"), (12, 1e9, ACCESS_3[2], "12 or more")]
    markers = {"rail": ("s", 15), "metro": ("o", 15), "tram": ("^", 15)}
    # Draw infrequent first so the frequent stations stay on top where they crowd.
    for low, high, colour, _ in bands:
        band = stations[(stations["departures_per_hr"] >= low) & (stations["departures_per_hr"] < high)]
        for mode, (marker, size) in markers.items():
            part = band[band["mode"] == mode]
            if not part.empty:
                ax.scatter(part.geometry.x, part.geometry.y, s=size, marker=marker, color=colour, edgecolor=PAPER,
                           linewidth=0.5, zorder=5)
    _finish_map(ax, data)
    theme.key_group(key, 0.0, "Peak departures per hour", [
        theme.point_handle(label, colour=colour) for *_, colour, label in bands
    ])
    theme.key_group(key, 0.33, "Station type", [
        theme.point_handle("Rail", marker="s", colour=INK_2), theme.point_handle("Metro (T-bane)" if data["slug"] == "oslo" else "Metro", colour=INK_2),
        theme.point_handle("Tram", marker="^", colour=INK_2),
    ])
    theme.key_group(key, 0.53, "Walking access", [
        theme.swatch(ACCESS[100], "Within a 15-minute walk of a station"),
        theme.swatch(STAGE, "Study urban area beyond that walk"),
        theme.point_handle(f"City centre ({data['centre_label']})", colour=PAPER, edge=INK, size=5),
    ])
    theme.credit(fig, theme.CREDIT_TRANSIT)
    theme.save(fig, out / f"{data['slug']}_rail_frequency.pdf")


def figure_coverage(data, out: Path):
    extent = data["extent_tight"]
    fig, axes, key = theme.map_figure(extent, n_panels=2, key_height_in=0.62)
    cmap, norm = theme.class_cmap(ACCESS_5, [0, 0.2, 0.4, 0.6, 0.8, 1.0001])
    panels = [
        ("a", data["coverage_rail"], "Rail, metro or tram within 15 minutes", data["pct_rail"]),
        ("b", data["coverage_frequent_any"], "Frequent service of any mode", data["pct_frequent_any"]),
    ]
    for ax, (letter, cells, title, pct) in zip(axes, panels):
        theme.base_map(ax, extent, data["water"], data["envelope"])
        cells.plot(ax=ax, column="covered_frac", cmap=cmap, norm=norm, linewidth=0, zorder=1, rasterized=True)
        _study_outline(ax, data, width=0.4, colour=INK_2, zorder=3)
        theme.centre_marker(ax, data["centre"])
        theme.panel_title(ax, letter, title, f"{pct:.1f}%")
    theme.scale_bar(axes[0], extent, length_m=4000)
    theme.class_ramp_key(key, 0.0, "Share of each cell’s residents covered", ACCESS_5,
                         ["0–20%", "20–40%", "40–60%", "60–80%", "80–100%"], cell_w=0.075, y=0.97)
    theme.key_group(key, 0.47, "How to read", [
        theme.outline_handle("Study urban area", colour=INK_2, width=0.6),
        theme.point_handle("City centre", colour=PAPER, edge=INK, size=4.5),
    ], y=0.97)
    key.text(0.70, 0.77, "Percentages: share of all urban residents.\nFrequent: at least 6 departures per hour.",
             transform=key.transAxes, ha="left", va="top", fontsize=6.4, color=INK_2, linespacing=1.35)
    theme.credit(fig, theme.CREDIT_TRANSIT)
    theme.save(fig, out / f"{data['slug']}_coverage.pdf")


def figure_travel_time(data, out: Path):
    matrix = data.get("travel_time")
    if matrix is None:
        print("skipping travel-time figure: no matrix")
        return
    cells = data["urban_cells"].copy()
    cells["id"] = cells["cell_id"].astype(str)
    cells = cells.merge(matrix[["from_id", "travel_time"]], left_on="id", right_on="from_id", how="left")
    fig, (ax,), key = theme.map_figure(data["extent"])
    _map_base(ax, data)
    colours = list(reversed(ACCESS_5))  # darker = quicker, as darker = better everywhere else
    cmap, norm = theme.class_cmap(colours, [0, 15, 30, 45, 60, 1e6])
    reached = cells[cells["travel_time"].notna()]
    reached.plot(ax=ax, column="travel_time", cmap=cmap, norm=norm, linewidth=0, zorder=1, rasterized=True)
    missing = cells[cells["travel_time"].isna()]
    if not missing.empty:
        missing.plot(ax=ax, color=PAPER, edgecolor=MUTED, hatch="/////", linewidth=0.2, zorder=1, rasterized=True)
    _study_outline(ax, data, width=0.5, colour=INK_2, zorder=3)
    _finish_map(ax, data)
    theme.class_ramp_key(key, 0.0, f"Median door-to-door time to {data['centre_label']}, minutes", colours,
                         ["<15", "15–30", "30–45", "45–60", "≥60"], cell_w=0.075, y=0.97)
    g = data["gtfs_summary"]
    key.text(0.47, 0.97, "THE MEDIAN RESIDENT", transform=key.transAxes, ha="left", va="top", fontsize=5.9,
             fontweight="semibold", color=INK_2)
    key.text(0.47, 0.70, f"{g['median_tt_centre_min']:.0f} min", transform=key.transAxes, ha="left", va="top",
             fontsize=13, fontweight="semibold", color=ACCESS[700])
    key.text(0.60, 0.70, f"{g['pct_centre_30min']:.0f}% arrive within 30 minutes\n{g['pct_centre_45min']:.0f}% within 45 minutes",
             transform=key.transAxes, ha="left", va="top", fontsize=6.6, color=INK, linespacing=1.35)
    if g.get("unreachable_centre_population_pct", 0) >= 0.1:  # only worth a key entry when visible
        key.text(0.83, 0.70, "Hatched: no trip found", transform=key.transAxes, ha="left", va="top", fontsize=6.2, color=INK_2)
    theme.credit(fig, theme.CREDIT_TRANSIT)
    theme.save(fig, out / f"{data['slug']}_travel_time.pdf")


def figure_summary(data, out: Path):
    """One-line-per-measure bar chart: the four access measures and centre reach."""
    import matplotlib.pyplot as plt

    g = data["gtfs_summary"]
    rows = [
        ("Walking access", None),
        ("Rail, metro or tram station within 15 minutes", data["pct_rail"]),
        ("… with at least 6 departures per hour", data["pct_rail_frequent"]),
        ("Any stop, including buses, within 500 m", data["pct_any_stop"]),
        ("Frequent service of any mode", data["pct_frequent_any"]),
        (f"Public-transport trip to the centre · median {g['median_tt_centre_min']:.0f} min", None),
        (f"{data['centre_label']} within 30 minutes", g["pct_centre_30min"]),
        (f"{data['centre_label']} within 45 minutes", g["pct_centre_45min"]),
    ]
    row_h = 0.215
    height = row_h * len(rows) + 0.35
    fig = plt.figure(figsize=(theme.TEXT_WIDTH_IN, height))
    ax = fig.add_axes([0.40, 0.1, 0.55, 0.86])
    n = len(rows)
    ax.set_ylim(n - 0.5, -0.6)
    ax.set_xlim(0, 100)
    for i, (label, value) in enumerate(rows):
        if value is None:
            ax.text(-0.715, i + 0.08, label.upper(), transform=ax.get_yaxis_transform(), ha="left", va="center",
                    fontsize=6.2, fontweight="semibold", color=INK_2)
            ax.plot([-0.715, 1.0], [i + 0.42, i + 0.42], transform=ax.get_yaxis_transform(), color=theme.HAIRLINE,
                    linewidth=0.6, clip_on=False)
            continue
        ax.text(-0.69, i, label, transform=ax.get_yaxis_transform(), ha="left", va="center", fontsize=7.4, color=INK)
        ax.barh(i, 100, height=0.5, color=ACCESS[50], linewidth=0, zorder=1)
        ax.barh(i, value, height=0.5, color=ACCESS[500], linewidth=0, zorder=2)
        ax.text(value + 1.2, i, f"{value:.1f}%", ha="left", va="center", fontsize=7.4, fontweight="semibold", color=INK, zorder=3)
    for x in (25, 50, 75):
        ax.axvline(x, color=PAPER, linewidth=0.8, zorder=2.5)
    ax.set_yticks([])
    ax.set_xticks([0, 25, 50, 75, 100], ["0", "25", "50", "75", "100%"])
    ax.tick_params(axis="x", length=0, pad=3, labelsize=6.4, colors=MUTED)
    ax.set_xlabel("Share of urban residents", loc="right", fontsize=6.2, color=MUTED, labelpad=1)
    for spine in ax.spines.values():
        spine.set_visible(False)
    theme.save(fig, out / f"{data['slug']}_summary.pdf")


# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------
def _threshold_areas(cells, urban, centre_point) -> dict:
    from urban_area.degurba import classify_degurba, select_study_centres
    from urban_area.geometry import build_adjacency_graph

    growth = urban.growth
    adjacency = build_adjacency_graph(cells, id_col="cell_id", mode="edge")
    result = {}
    for threshold in SENSITIVITY_THRESHOLDS:
        classified = classify_degurba(
            cells, adjacency, centre_density_min=threshold, cluster_density_min=growth.cluster_density_min,
            builtup_share_min=growth.builtup_share_min, centre_min_population=growth.centre_min_population,
            cluster_min_population=growth.cluster_min_population, gap_fill_min_neighbors=growth.gap_fill_min_neighbors,
            max_hole_area_sqkm=growth.max_hole_area_sqkm,
        )
        keep = select_study_centres(classified, centre_point=centre_point)
        chosen = classified[classified["cell_id"].isin(keep)]
        result[threshold] = {
            "geometry": unary_union(list(chosen.geometry)),
            "area_km2": chosen.geometry.area.sum() / 1e6,
            "population": chosen["population"].sum(),
        }
    return result


def load_data(config_path: Path, city: str) -> dict:
    from shapely.geometry import Point
    from urban_area.io import load_city_boundary

    urban = load_city_config(config_path, city_name=city)
    transport = load_transport_config(config_path, city)
    crs = transport.resolved_metric_crs()
    slug = transport.resolved_slug()

    def layer(path, name):
        return gpd.read_file(path, layer=name).to_crs(crs) if name in list_layers(path) else None

    prov = transport.data_dir / "provenance"
    validation = json.loads((prov / f"{slug}_transport_validation.json").read_text(encoding="utf-8"))
    gtfs_path = prov / f"{slug}_gtfs_validation.json"
    gtfs_summary = json.loads(gtfs_path.read_text(encoding="utf-8"))["summary"] if gtfs_path.exists() else {}
    urban_validation = json.loads((prov / f"{slug}_growing_refined_validation.json").read_text(encoding="utf-8"))

    hull = layer(urban.urban_areas_gpkg, f"urban_area_{slug}_growing_refined")
    analysis = layer(urban.population_gpkg, f"population_density_with_buildings_roads_{slug}")
    centre_point = gpd.GeoSeries([Point(urban.centre_lon, urban.centre_lat)], crs="EPSG:4326").to_crs(crs).iloc[0]
    threshold_areas = _threshold_areas(analysis, urban, centre_point)

    # One frame for every map: the study area and the loosest-threshold centre, plus a margin.
    envelope, _ = load_city_boundary(urban)
    envelope = envelope.to_crs(crs)
    frame = unary_union([hull.union_all(), threshold_areas[min(SENSITIVITY_THRESHOLDS)]["geometry"]])
    extent = _fit_frame(frame.bounds, envelope)
    # Two-panel figures are half as wide, so they frame the study area alone.
    extent_tight = _fit_frame(hull.total_bounds, envelope, pad=700)

    labels = []
    for item in MAP_LABELS.get(slug, []):
        xy = gpd.GeoSeries([Point(*item["lonlat"])], crs="EPSG:4326").to_crs(crs).iloc[0]
        labels.append({**item, "xy": (xy.x, xy.y)})

    isochrones = layer(transport.isochrone_gpkg, f"isochrones_15min_{slug}")
    stations = layer(transport.rail_gpkg, f"rail_stations_{slug}")
    if "keep_station" in stations.columns:
        stations = stations[stations["keep_station"].astype(bool)]
    travel_path = transport.data_dir / f"travel_time_to_centre_{slug}.parquet"
    classes = urban_validation["degurba_classes"]
    return {
        "slug": slug,
        "crs": crs,
        "city": city.split(",")[0],
        "analysis": analysis,
        "urban_cells": layer(urban.urban_areas_gpkg, f"urban_cells_{slug}_growing_refined"),
        "hull": hull,
        "hull_display": gpd.GeoSeries([_generalise(hull.union_all())], crs=crs),
        "envelope": envelope,
        "water": layer(urban.water_mask.water_gpkg, urban.water_mask.resolved_layer(slug)),
        "extent": extent,
        "extent_tight": extent_tight,
        "labels": labels,
        "centre": (centre_point.x, centre_point.y),
        "centre_label": CENTRE_SHORT_LABEL.get(slug, "City centre"),
        "threshold_areas": threshold_areas,
        "class_summary": {name: values["population"] for name, values in classes.items()},
        "urban_pop": urban_validation["summary"]["urban_population"],
        "stations": stations,
        "rail_lines": layer(transport.rail_gpkg, f"rail_lines_{slug}"),
        "isochrones_union": gpd.GeoSeries([isochrones.union_all()], crs=crs),
        "coverage_rail": layer(transport.isochrone_gpkg, f"coverage_cells_15min_{slug}"),
        "coverage_frequent_any": layer(transport.isochrone_gpkg, f"coverage_cells_frequent_any_mode_{slug}"),
        "pct_rail": validation["coverage"]["pct_within_15min"],
        "pct_rail_frequent": validation["frequency"]["pct_within_15min_frequent"],
        "pct_any_stop": validation["all_modes"]["pct_any_stop"],
        "pct_frequent_any": validation["all_modes"]["pct_frequent_any_mode"],
        "service_date": validation["frequency"]["service_date"],
        "gtfs_summary": gtfs_summary,
        "travel_time": pd.read_parquet(travel_path) if travel_path.exists() else None,
    }


FIGURES = {
    "degurba": figure_degurba,
    "sensitivity": figure_sensitivity,
    "rail": figure_rail,
    "coverage": figure_coverage,
    "travel": figure_travel_time,
    "summary": figure_summary,
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Render report figures for one city.")
    parser.add_argument("--city", default="Oslo, Norway")
    parser.add_argument("--config", type=Path, default=Path("city_config.example.json"))
    parser.add_argument("--output", type=Path, default=Path("../Report/Images"))
    parser.add_argument("--only", nargs="*", choices=sorted(FIGURES), help="Render only these figures.")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    data = load_data(args.config, args.city)
    for name in args.only or FIGURES:
        result = FIGURES[name](data, args.output)
        if isinstance(result, pd.DataFrame):
            print(result.to_string(index=False))


if __name__ == "__main__":
    main()
