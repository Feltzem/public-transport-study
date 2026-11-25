"""Render one city's report figures from the working outputs.

Run from Code/: python make_report_figures.py --city "Helsinki, Finland"
Figures are written to ../Report/Images as {slug}_*.pdf plus PNG previews.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import geopandas as gpd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.colors import BoundaryNorm, ListedColormap
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
import pandas as pd

from transport.config import load_transport_config
from transport.io import list_layers
from urban_area import load_city_config

# Reference palette (light mode, print).
SURFACE = "#fcfcfb"
TEXT = "#0b0b0b"
TEXT_SECONDARY = "#52514e"
WATER = "#c9d8e6"
BLUE = {100: "#cde2fb", 200: "#9ec5f4", 250: "#86b6ef", 300: "#6da7ec", 400: "#3987e5",
        450: "#2a78d6", 500: "#256abf", 550: "#1c5cab", 600: "#184f95", 650: "#104281", 700: "#0d366b"}
ORANGE = "#eb6834"
plt.rcParams.update({
    "font.family": "DejaVu Sans", "font.size": 9, "text.color": TEXT, "axes.labelcolor": TEXT_SECONDARY,
    "axes.edgecolor": TEXT_SECONDARY, "xtick.color": TEXT_SECONDARY, "ytick.color": TEXT_SECONDARY,
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
})


LAND = "#f1f0ec"


def _base_map(ax, extent, water, land=None):
    ax.set_xlim(extent[0], extent[2])
    ax.set_ylim(extent[1], extent[3])
    ax.set_facecolor(SURFACE)
    # Grey marks populated cells; white is unpopulated land or the fjord (the OSM
    # mask holds lakes and rivers but only a thin strip along the coastline).
    if land is not None:
        land.plot(ax=ax, color=LAND, linewidth=0, zorder=0, rasterized=True)
    if water is not None and not water.empty:
        water.plot(ax=ax, color=WATER, linewidth=0, zorder=0.5, rasterized=True)
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.set_xticks([])
    ax.set_yticks([])
    ax.set_aspect("equal")
    # 5 km scale bar in the lower left.
    x0 = extent[0] + 0.04 * (extent[2] - extent[0])
    y0 = extent[1] + 0.04 * (extent[3] - extent[1])
    ax.plot([x0, x0 + 5000], [y0, y0], color=TEXT, linewidth=2, solid_capstyle="butt", zorder=10)
    ax.text(x0 + 2500, y0 + 400, "5 km", ha="center", va="bottom", fontsize=8, color=TEXT, zorder=10)


SHOW_TITLES = False  # the LaTeX captions carry titles; set True for standalone use


def _save(fig, path: Path, title: str, subtitle: str | None = None):
    if SHOW_TITLES:
        fig.suptitle(title, x=0.02, ha="left", fontsize=11, fontweight="bold", color=TEXT)
        if subtitle:
            fig.text(0.02, 0.925, subtitle, ha="left", fontsize=8.5, color=TEXT_SECONDARY)
    fig.savefig(path, bbox_inches="tight", dpi=300)
    fig.savefig(path.with_suffix(".png"), bbox_inches="tight", dpi=110)
    plt.close(fig)
    print(f"wrote {path}")


def figure_degurba(data, out: Path):
    cells, hull, water, extent = data["analysis"], data["hull"], data["water"], data["extent"]
    fig, ax = plt.subplots(figsize=(7.2, 6.0))
    _base_map(ax, extent, water, data["land"])
    colours = {"rural": "#eeeeec", "urban_cluster": BLUE[250], "urban_centre": BLUE[550]}
    for name, colour in colours.items():
        part = cells[cells["degurba_class"] == name]
        if not part.empty:
            # Rural cells sit under the water layer so the fjord still reads as water.
            part.plot(ax=ax, color=colour, edgecolor=SURFACE, linewidth=0.15, zorder=0.2 if name == "rural" else 1, rasterized=True)
    hull.boundary.plot(ax=ax, color=ORANGE, linewidth=1.4, zorder=3)
    ax.plot(*data["centre"], marker="o", color=TEXT, markersize=4, zorder=4)
    handles = [
        Patch(facecolor=colours["urban_centre"], label="Urban centre (\u22651,500/km\u00b2, \u226550k)"),
        Patch(facecolor=colours["urban_cluster"], label="Urban cluster (\u2265300/km\u00b2, \u22655k)"),
        Patch(facecolor=colours["rural"], edgecolor="#cccccc", label="Rural"),
        Line2D([], [], color=ORANGE, linewidth=1.4, label="Study urban area (refined)"),
        Line2D([], [], marker="o", color=TEXT, linestyle="", markersize=4, label="City centre"),
    ]
    ax.legend(handles=handles, loc="upper right", frameon=True, facecolor=SURFACE, edgecolor="#dddddd", fontsize=7.5)
    _save(fig, out / f"{data['slug']}_degurba.pdf", f"Degree of Urbanisation classes, {data['city']} envelope",
          "Kontur H3 cells; densities on land area; outline after edge refinement and land clipping")


def figure_sensitivity(data, out: Path):
    from urban_area.degurba import classify_degurba, select_study_centres
    from urban_area.geometry import build_adjacency_graph
    from shapely.ops import unary_union

    cells, water = data["analysis"], data["water"]
    loose = classify_degurba(cells, build_adjacency_graph(cells, id_col="cell_id", mode="edge"), centre_density_min=1200)
    minx, miny, maxx, maxy = loose[loose["degurba_centre_id"] >= 0].total_bounds
    extent = (minx - 1500, miny - 1500, maxx + 1500, maxy + 1500)
    adjacency = build_adjacency_graph(cells, id_col="cell_id", mode="edge")
    fig, ax = plt.subplots(figsize=(7.2, 6.0))
    _base_map(ax, extent, water, data["land"])
    styles = [(2000, BLUE[700], "-"), (1500, BLUE[450], "-"), (1200, BLUE[250], "--")]
    handles = []
    rows = []
    for threshold, colour, style in styles:
        classified = classify_degurba(cells, adjacency, centre_density_min=threshold)
        keep = select_study_centres(classified, centre_point=data["centre_point"])
        chosen = classified[classified["cell_id"].isin(keep)]
        outline = unary_union(list(chosen.geometry))
        gpd.GeoSeries([outline], crs=cells.crs).boundary.plot(ax=ax, color=colour, linewidth=1.3, linestyle=style, zorder=3)
        label = f"{threshold:,}/km\u00b2: {chosen.geometry.area.sum() / 1e6:,.0f} km\u00b2, {chosen['population'].sum() / 1e3:,.0f}k people"
        handles.append(Line2D([], [], color=colour, linestyle=style, linewidth=1.3, label=label))
        rows.append({"threshold": threshold, "area_km2": chosen.geometry.area.sum() / 1e6, "population": chosen["population"].sum()})
    ax.legend(handles=handles, loc="upper right", frameon=True, facecolor=SURFACE, edgecolor="#dddddd", fontsize=7.5,
              title="Urban-centre density threshold", title_fontsize=7.5)
    _save(fig, out / f"{data['slug']}_sensitivity.pdf", "Urban centre under alternative density thresholds",
          "Unrefined H3 cells; the study uses 1,500 inhabitants per km\u00b2 of land")
    return pd.DataFrame(rows)


def figure_rail(data, out: Path):
    stations, catchments, lines = data["stations"], data["isochrones"], data["rail_lines"]
    fig, ax = plt.subplots(figsize=(7.2, 6.0))
    _base_map(ax, data["extent"], data["water"], data["land"])
    data["hull"].plot(ax=ax, color="#e4e2da", linewidth=0, zorder=1, rasterized=True)
    catchments.plot(ax=ax, color=BLUE[100], alpha=0.6, linewidth=0, zorder=2, rasterized=True)
    lines.plot(ax=ax, color=TEXT_SECONDARY, linewidth=0.4, zorder=3)
    bands = [(0, 6, BLUE[250], "<6 per hour"), (6, 12, BLUE[450], "6\u201312 per hour"), (12, 1e9, BLUE[650], "\u226512 per hour")]
    markers = {"rail": "s", "metro": "o", "tram": "^"}
    for low, high, colour, _ in bands:
        band = stations[(stations["departures_per_hr"] >= low) & (stations["departures_per_hr"] < high)]
        for mode, marker in markers.items():
            part = band[band["mode"] == mode]
            if not part.empty:
                ax.scatter(part.geometry.x, part.geometry.y, s=18, marker=marker, color=colour,
                           edgecolor=SURFACE, linewidth=0.6, zorder=5)
    handles = [Patch(facecolor=BLUE[100], label="15-minute walking catchment")]
    handles += [Line2D([], [], marker="o", linestyle="", color=colour, markersize=6, label=label) for *_, colour, label in bands]
    handles += [Line2D([], [], marker=marker, linestyle="", color=TEXT_SECONDARY, markersize=6, label=mode.capitalize())
                for mode, marker in markers.items()]
    ax.legend(handles=handles, loc="upper right", frameon=True, facecolor=SURFACE, edgecolor="#dddddd", fontsize=7.5,
              title="Departures, busiest direction, 07\u201309", title_fontsize=7.5)
    _save(fig, out / f"{data['slug']}_rail_frequency.pdf", "Rail, metro and tram stations by peak frequency",
          f"GTFS service date {data['service_date']}; stations retained by walking-catchment population")


def figure_coverage(data, out: Path):
    fig, axes = plt.subplots(1, 2, figsize=(10.5, 5.2))
    cmap = ListedColormap([BLUE[100], BLUE[250], BLUE[400], BLUE[550], BLUE[700]])
    norm = BoundaryNorm([0, 0.2, 0.4, 0.6, 0.8, 1.0001], cmap.N)
    panels = [
        (data["coverage_rail"], "Rail, metro or tram station within 15 minutes", data["pct_rail"]),
        (data["coverage_frequent_any"], "Frequent service of any mode (\u22656/hour)", data["pct_frequent_any"]),
    ]
    for ax, (cells, title, pct) in zip(axes, panels):
        _base_map(ax, data["extent"], data["water"], data["land"])
        cells.plot(ax=ax, column="covered_frac", cmap=cmap, norm=norm, linewidth=0, zorder=1, rasterized=True)
        data["hull"].boundary.plot(ax=ax, color=TEXT_SECONDARY, linewidth=0.5, zorder=3)
        ax.set_title(f"{title}\n{pct:.1f}% of urban residents", fontsize=9, color=TEXT, loc="left")
    handles = [Patch(facecolor=cmap(i), label=label) for i, label in enumerate(["0\u201320%", "20\u201340%", "40\u201360%", "60\u201380%", "80\u2013100%"])]
    fig.legend(handles=handles, loc="lower center", ncol=5, frameon=False, fontsize=8, title="Share of each cell\u2019s residents covered", title_fontsize=8)
    fig.subplots_adjust(bottom=0.12, wspace=0.02)
    _save(fig, out / f"{data['slug']}_coverage.pdf", f"Population coverage within the {data['city']} urban area",
          "Building-weighted share of each cell\u2019s residents inside the dissolved catchment")


def figure_travel_time(data, out: Path):
    matrix = data.get("travel_time")
    if matrix is None:
        print("skipping travel-time figure: no matrix")
        return
    cells = data["urban_cells"].copy()
    cells["id"] = cells["cell_id"].astype(str)
    cells = cells.merge(matrix[["from_id", "travel_time"]], left_on="id", right_on="from_id", how="left")
    fig, ax = plt.subplots(figsize=(7.2, 6.0))
    _base_map(ax, data["extent"], data["water"], data["land"])
    cmap = ListedColormap([BLUE[100], BLUE[250], BLUE[400], BLUE[550], BLUE[700]])
    norm = BoundaryNorm([0, 15, 30, 45, 60, 1e6], cmap.N)
    cells[cells["travel_time"].notna()].plot(ax=ax, column="travel_time", cmap=cmap, norm=norm, linewidth=0, zorder=1, rasterized=True)
    if cells["travel_time"].isna().any():
        cells[cells["travel_time"].isna()].plot(ax=ax, color="#bbbbbb", linewidth=0, zorder=1, rasterized=True)
    ax.plot(*data["centre"], marker="o", color=ORANGE, markersize=5, zorder=4)
    handles = [Patch(facecolor=cmap(i), label=label) for i, label in enumerate(["<15 min", "15\u201330", "30\u201345", "45\u201360", "\u226560"])]
    handles.append(Patch(facecolor="#bbbbbb", label="No trip found"))
    handles.append(Line2D([], [], marker="o", linestyle="", color=ORANGE, markersize=5, label="City centre"))
    ax.legend(handles=handles, loc="upper right", frameon=True, facecolor=SURFACE, edgecolor="#dddddd", fontsize=7.5,
              title="Median door-to-door time", title_fontsize=7.5)
    _save(fig, out / f"{data['slug']}_travel_time.pdf", f"Public-transport travel time to central {data['city']}",
          f"r5py, walk + transit, median over departures 07:00\u201309:00 on {data['service_date']}")


def load_data(config_path: Path, city: str) -> dict:
    import json

    from shapely.geometry import Point

    urban = load_city_config(config_path, city_name=city)
    transport = load_transport_config(config_path, city)
    crs = transport.resolved_metric_crs()
    slug = transport.resolved_slug()

    def layer(path, name):
        return gpd.read_file(path, layer=name).to_crs(crs) if name in list_layers(path) else None

    hull = layer(urban.urban_areas_gpkg, f"urban_area_{slug}_growing_refined")
    minx, miny, maxx, maxy = hull.total_bounds
    pad = 1500
    extent = (minx - pad, miny - pad, maxx + pad, maxy + pad)
    centre_point = gpd.GeoSeries([Point(urban.centre_lon, urban.centre_lat)], crs="EPSG:4326").to_crs(crs).iloc[0]
    validation = json.loads((transport.data_dir / "provenance" / f"{slug}_transport_validation.json").read_text(encoding="utf-8"))
    travel_path = transport.data_dir / f"travel_time_to_centre_{slug}.parquet"
    return {
        "slug": slug,
        "city": city.split(",")[0],
        "analysis": layer(urban.population_gpkg, f"population_density_with_buildings_roads_{slug}"),
        "urban_cells": layer(urban.urban_areas_gpkg, f"urban_cells_{slug}_growing_refined"),
        "hull": hull,
        "water": layer(urban.water_mask.water_gpkg, urban.water_mask.resolved_layer(slug)),
        "land": layer(urban.population_gpkg, f"population_density_with_buildings_roads_{slug}"),
        "extent": extent,
        "centre_point": centre_point,
        "centre": (centre_point.x, centre_point.y),
        "stations": layer(transport.rail_gpkg, f"rail_stations_{slug}"),
        "rail_lines": layer(transport.rail_gpkg, f"rail_lines_{slug}"),
        "isochrones": layer(transport.isochrone_gpkg, f"isochrones_15min_{slug}"),
        "coverage_rail": layer(transport.isochrone_gpkg, f"coverage_cells_15min_{slug}"),
        "coverage_frequent_any": layer(transport.isochrone_gpkg, f"coverage_cells_frequent_any_mode_{slug}"),
        "pct_rail": validation["coverage"]["pct_within_15min"],
        "pct_frequent_any": validation["all_modes"]["pct_frequent_any_mode"],
        "service_date": validation["frequency"]["service_date"],
        "travel_time": pd.read_parquet(travel_path) if travel_path.exists() else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Render report figures for one city.")
    parser.add_argument("--city", default="Oslo, Norway")
    parser.add_argument("--config", type=Path, default=Path("city_config.example.json"))
    parser.add_argument("--output", type=Path, default=Path("../Report/Images"))
    parser.add_argument("--titles", action="store_true", help="Draw titles inside the figures.")
    args = parser.parse_args()
    global SHOW_TITLES
    SHOW_TITLES = args.titles
    args.output.mkdir(parents=True, exist_ok=True)
    data = load_data(args.config, args.city)
    figure_degurba(data, args.output)
    sensitivity = figure_sensitivity(data, args.output)
    print(sensitivity.to_string(index=False))
    figure_rail(data, args.output)
    figure_coverage(data, args.output)
    figure_travel_time(data, args.output)


if __name__ == "__main__":
    main()
