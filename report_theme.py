"""Flat visual theme shared by every report graphic.

One set of tokens drives the matplotlib figures and the LaTeX report (the
same hex values are declared as xcolor colours in Report/theme.sty), so maps,
charts, captions and headings read as one system.

Colour roles
------------
* Base map: warm-grey land, pale blue-grey water, white paper. No relief, no
  shadows, no gradients: every layer is a flat fill or a hairline.
* ACCESS (indigo): public-transport access. One hue, light to dark, and darker
  always means *better served*: higher coverage, more departures, shorter trips.
  Indigo is used because it can never be mistaken for water.
* CLAY (terracotta): urban form (Degree of Urbanisation classes, thresholds).
* INK: the study urban-area outline, the city-centre marker, text.

Typography
----------
Source Sans 3 (shipped with MiKTeX) at final print size: figures are drawn at
the LaTeX text width and included at 100%, so 7.5 pt in a figure is 7.5 pt on
the page.
"""

from __future__ import annotations

from pathlib import Path
import glob
import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.font_manager as font_manager
import matplotlib.patheffects as path_effects
import matplotlib.pyplot as plt
from matplotlib.colors import BoundaryNorm, ListedColormap
from matplotlib.lines import Line2D
from matplotlib.patches import Patch, Polygon as MplPolygon, Rectangle

# --------------------------------------------------------------------------
# Tokens. Keep in sync with Report/theme.sty.
# --------------------------------------------------------------------------
PAPER = "#ffffff"
LAND = "#f0ede7"          # land inside the map frame
STAGE = "#dcd5ca"         # the study urban area, where it is a backdrop for data
WATER = "#c9dcea"
WATER_INK = "#5f85a1"     # water labels
INK = "#1e1f25"           # primary text, outlines, centre marker
INK_2 = "#585550"         # secondary text
MUTED = "#898681"         # credits, scale-bar labels
HAIRLINE = "#d8d4cd"      # rules, grid
RAIL_LINE = "#6e6b67"     # track geometry (context, not data)

# Access: indigo, OKLCH hue ~285, evenly stepped in lightness (L 0.95 -> 0.28).
ACCESS = {
    50: "#f1f2ff", 100: "#dee0fc", 200: "#bfc2f4", 300: "#9da0e5", 400: "#7e7ed2",
    500: "#6462b7", 600: "#4f4d98", 700: "#3b3975", 800: "#282754",
}
# Urban form: clay, OKLCH hue 45, lightness 0.94 -> 0.40.
CLAY = {
    100: "#fee5db", 200: "#f4cab7", 300: "#e6a68a", 400: "#d48460", 500: "#bc653b",
    600: "#974a24", 700: "#6e351a",
}

# Five-class access scale used by every choropleth (light = worst, dark = best).
ACCESS_5 = [ACCESS[100], ACCESS[300], ACCESS[400], ACCESS[600], ACCESS[800]]
# Three-class ordinal scale for point symbols (light end clears 2:1 on LAND).
ACCESS_3 = [ACCESS[300], ACCESS[500], ACCESS[800]]

TEXT_WIDTH_IN = 6.46      # A4, 2.3 cm margins: \textwidth in Report/main.tex
CREDIT_URBAN = "Population: Kontur \u00b7 Buildings: Microsoft \u00b7 Map data \u00a9 OpenStreetMap contributors"
CREDIT_TRANSIT = "Timetables: Entur GTFS \u00b7 Map data \u00a9 OpenStreetMap contributors"


def transit_credit(feed: str) -> str:
    """Credit line for maps drawn from one city's timetable feed."""
    return f"Timetables: {feed} \u00b7 Map data \u00a9 OpenStreetMap contributors"

FONT_DIRS = [
    os.environ.get("REPORT_FONT_DIR", ""),
    "C:/Users/feltz/AppData/Local/Programs/MiKTeX/fonts/opentype/adobe/sourcesans",
    "/usr/share/texmf/fonts/opentype/adobe/sourcesans",
    "/usr/share/texlive/texmf-dist/fonts/opentype/adobe/sourcesans",
]
FONT_FAMILY = "Source Sans 3"


FONT_CACHE = Path(__file__).resolve().parent / "cache" / "fonts_ttf"
FONT_WEIGHTS = ("Regular", "RegularIt", "Semibold", "SemiboldIt", "Bold")


def _otf_to_ttf(src: str, dst: Path) -> None:
    """Convert a CFF-outline OpenType font to TrueType outlines.

    Matplotlib embeds TrueType as Type 42, which every PDF reader accepts;
    CFF-flavoured OpenType ends up as FontFile3/OpenType, which needs PDF 1.6
    and makes poppler warn when the figure is included by pdflatex (PDF 1.5).
    """
    from fontTools.pens.cu2quPen import Cu2QuPen
    from fontTools.pens.ttGlyphPen import TTGlyphPen
    from fontTools.ttLib import TTFont, newTable

    font = TTFont(src)
    order = font.getGlyphOrder()
    glyph_set = font.getGlyphSet()
    glyf = newTable("glyf")
    glyf.glyphOrder = order
    glyf.glyphs = {}
    for name in order:
        pen = TTGlyphPen(glyph_set)
        glyph_set[name].draw(Cu2QuPen(pen, 1.0, reverse_direction=True))
        glyf[name] = pen.glyph()
    font["loca"] = newTable("loca")
    font["glyf"] = glyf
    maxp = newTable("maxp")
    maxp.tableVersion = 0x00010000
    for attr in ("maxZones", "maxTwilightPoints", "maxStorage", "maxFunctionDefs", "maxInstructionDefs",
                 "maxStackElements", "maxSizeOfInstructions", "maxComponentElements"):
        setattr(maxp, attr, 1 if attr == "maxZones" else 0)
    font["maxp"] = maxp
    font["head"].glyphDataFormat = 0
    post = font["post"]
    post.formatType = 2.0
    post.extraNames = []
    post.mapping = {}
    post.glyphOrder = order
    for tag in ("CFF ", "VORG"):
        if tag in font:
            del font[tag]
    font.sfntVersion = "\000\001\000\000"
    dst.parent.mkdir(parents=True, exist_ok=True)
    font.save(str(dst))


def _register_fonts() -> str:
    for directory in FONT_DIRS:
        sources = [os.path.join(directory, f"SourceSans3-{w}.otf") for w in FONT_WEIGHTS] if directory else []
        sources = [s for s in sources if os.path.exists(s)]
        if not sources:
            continue
        for src in sources:
            ttf = FONT_CACHE / (Path(src).stem + ".ttf")
            if not ttf.exists():
                _otf_to_ttf(src, ttf)
            font_manager.fontManager.addfont(str(ttf))
        return FONT_FAMILY
    return "DejaVu Sans"  # fallback keeps the script runnable without MiKTeX


def apply() -> None:
    """Install the theme into matplotlib's rcParams."""
    family = _register_fonts()
    plt.rcParams.update({
        "font.family": family,
        "font.size": 7.5,
        "pdf.fonttype": 42,
        "text.color": INK,
        "axes.labelcolor": INK_2,
        "axes.edgecolor": HAIRLINE,
        "axes.linewidth": 0.6,
        "axes.titlesize": 8.5,
        "axes.titleweight": "semibold",
        "axes.titlecolor": INK,
        "xtick.color": INK_2,
        "ytick.color": INK_2,
        "xtick.labelsize": 7,
        "ytick.labelsize": 7,
        "legend.fontsize": 7,
        "legend.title_fontsize": 7,
        "figure.facecolor": PAPER,
        "axes.facecolor": PAPER,
        "savefig.facecolor": PAPER,
        "hatch.linewidth": 0.4,
    })


def halo(width: float = 2.2, colour: str = LAND):
    return [path_effects.withStroke(linewidth=width, foreground=colour)]


# --------------------------------------------------------------------------
# Figure scaffolding
# --------------------------------------------------------------------------
KEY_HEIGHT_IN = 0.62     # key strip above the map(s)
FOOT_HEIGHT_IN = 0.16    # credit line below the map(s)


def map_figure(extent, n_panels: int = 1, gap_in: float = 0.12, key_height_in: float = KEY_HEIGHT_IN):
    """Text-width figure: a key strip on top, n equal map panels, a credit line.

    Panel height follows the extent's aspect ratio so the map is never stretched.
    Returns (fig, axes, key_ax); ``axes`` is a list even for one panel.
    """
    width = TEXT_WIDTH_IN
    panel_w = (width - gap_in * (n_panels - 1)) / n_panels
    aspect = (extent[3] - extent[1]) / (extent[2] - extent[0])
    panel_h = panel_w * aspect
    height = key_height_in + panel_h + FOOT_HEIGHT_IN
    fig = plt.figure(figsize=(width, height))
    axes = []
    for i in range(n_panels):
        left = i * (panel_w + gap_in) / width
        ax = fig.add_axes([left, FOOT_HEIGHT_IN / height, panel_w / width, panel_h / height])
        axes.append(ax)
    key_ax = fig.add_axes([0, (FOOT_HEIGHT_IN + panel_h) / height, 1, key_height_in / height])
    key_ax.axis("off")
    return fig, axes, key_ax


def base_map(ax, extent, water=None, envelope=None):
    """Flat base: paper outside the study envelope, land inside, water on top."""
    ax.set_xlim(extent[0], extent[2])
    ax.set_ylim(extent[1], extent[3])
    ax.set_aspect("equal")
    ax.set_facecolor(PAPER if envelope is not None else LAND)
    if envelope is not None:
        envelope.plot(ax=ax, color=LAND, linewidth=0, zorder=0)
    if water is not None and not water.empty:
        water.plot(ax=ax, color=WATER, linewidth=0, zorder=0.5, rasterized=True)
    for spine in ax.spines.values():
        spine.set_visible(False)
    ax.set_xticks([])
    ax.set_yticks([])


def scale_bar(ax, extent, length_m: float = 5000, north: bool = True):
    """Two-segment flat scale bar (0 / half / full) with a small north arrow."""
    w, h = extent[2] - extent[0], extent[3] - extent[1]
    x0, y0 = extent[0] + 0.025 * w, extent[1] + 0.03 * h
    bar_h = 0.006 * h
    ax.add_patch(Rectangle((x0, y0), length_m / 2, bar_h, facecolor=INK, edgecolor=INK, linewidth=0.5, zorder=20))
    ax.add_patch(Rectangle((x0 + length_m / 2, y0), length_m / 2, bar_h, facecolor=PAPER, edgecolor=INK, linewidth=0.5, zorder=20))
    ticks = [(0, "0"), (1, f"{length_m / 1000:g} km")]
    if length_m / w > 0.09:  # room for a middle label
        ticks.insert(1, (0.5, f"{length_m / 2000:g}"))
    for frac, label in ticks:
        ax.text(x0 + frac * length_m, y0 + bar_h * 2.2, label, ha="center", va="bottom", fontsize=6.2, color=INK_2,
                zorder=20, path_effects=halo(1.6))
    if north:
        nx, ny = x0 + length_m + 0.055 * w, y0
        s = 0.022 * h
        ax.add_patch(MplPolygon([(nx - s * 0.32, ny), (nx, ny + s), (nx + s * 0.32, ny), (nx, ny + s * 0.28)],
                                closed=True, facecolor=INK, edgecolor="none", zorder=20))
        ax.text(nx, ny + s * 1.12, "N", ha="center", va="bottom", fontsize=6.2, fontweight="semibold", color=INK,
                zorder=20, path_effects=halo(1.6))


def credit(fig, text: str):
    fig.text(0.998, 0.004, text, ha="right", va="bottom", fontsize=5.6, color=MUTED)


def place_labels(ax, labels, extent, kind: str = "place"):
    """Place names (semibold, ink-2) or water names (italic, water ink)."""
    for item in labels:
        x, y = item["xy"]
        if not (extent[0] < x < extent[2] and extent[1] < y < extent[3]):
            continue
        if item.get("kind", "place") == "water":
            ax.text(x, y, item["name"], ha="center", va="center", fontsize=6.8, style="italic", color=WATER_INK,
                    zorder=15, rotation=item.get("rotation", 0))
        else:
            ax.text(x, y, item["name"].upper(), ha=item.get("ha", "center"), va="center", fontsize=5.6,
                    fontweight="semibold", color=INK_2, zorder=15, path_effects=halo(1.8, PAPER))


def centre_marker(ax, xy, label: str | None = None, label_side: str = "right"):
    ax.scatter([xy[0]], [xy[1]], s=26, marker="o", facecolor=PAPER, edgecolor=INK, linewidth=1.3, zorder=30)
    ax.scatter([xy[0]], [xy[1]], s=4, marker="o", facecolor=INK, edgecolor="none", zorder=31)
    if label:
        dx = 420 if label_side == "right" else -420
        ax.text(xy[0] + dx, xy[1], label, ha="left" if label_side == "right" else "right", va="center", fontsize=6.6,
                fontweight="semibold", color=INK, zorder=31, path_effects=halo(2.0, PAPER))


def panel_title(ax, letter: str, title: str, value: str | None = None):
    """Panel heading above a map: 'a  Title' with an optional headline value."""
    ax.text(0.0, 1.015, letter, transform=ax.transAxes, ha="left", va="bottom", fontsize=8.5, fontweight="bold",
            color=ACCESS[600])
    ax.text(0.045, 1.015, title, transform=ax.transAxes, ha="left", va="bottom", fontsize=7.6, fontweight="semibold",
            color=INK)
    if value:
        ax.text(1.0, 1.015, value, transform=ax.transAxes, ha="right", va="bottom", fontsize=7.6, fontweight="semibold",
                color=ACCESS[600])


# --------------------------------------------------------------------------
# Key (legend) strip
# --------------------------------------------------------------------------
def key_heading(key_ax, x: float, text: str, y: float = 0.9):
    key_ax.text(x, y, text.upper(), transform=key_ax.transAxes, ha="left", va="top", fontsize=5.9,
                fontweight="semibold", color=INK_2)


def key_group(key_ax, x: float, heading: str, handles, ncol: int = 1, y: float = 0.9, **kwargs):
    """A headed block of legend entries anchored at (x, y) in the key strip."""
    key_heading(key_ax, x, heading, y=y)
    legend = key_ax.legend(handles=handles, loc="upper left", bbox_to_anchor=(x - 0.006, y - 0.2), ncol=ncol,
                           frameon=False, borderaxespad=0, handlelength=1.6, handleheight=0.9, handletextpad=0.5,
                           columnspacing=1.2, labelspacing=0.35, fontsize=6.8, labelcolor=INK, **kwargs)
    key_ax.add_artist(legend)
    return legend


def class_ramp_key(key_ax, x: float, heading: str, colours, labels, y: float = 0.9, cell_w: float = 0.07,
                   cell_h: float = 0.17, gap: float = 0.004, low_note: str | None = None, high_note: str | None = None):
    """Joined horizontal swatches with labels under each (choropleth key)."""
    key_heading(key_ax, x, heading, y=y)
    top = y - 0.27
    for i, (colour, label) in enumerate(zip(colours, labels)):
        x0 = x + i * (cell_w + gap)
        key_ax.add_patch(Rectangle((x0, top - cell_h), cell_w, cell_h, transform=key_ax.transAxes, facecolor=colour,
                                   edgecolor="none"))
        key_ax.text(x0 + cell_w / 2, top - cell_h - 0.06, label, transform=key_ax.transAxes, ha="center", va="top",
                    fontsize=6.4, color=INK)
    right = x + len(colours) * (cell_w + gap) - gap
    if low_note:
        key_ax.text(x, top + 0.04, low_note, transform=key_ax.transAxes, ha="left", va="bottom", fontsize=5.8,
                    color=MUTED, style="italic")
    if high_note:
        key_ax.text(right, top + 0.04, high_note, transform=key_ax.transAxes, ha="right", va="bottom", fontsize=5.8,
                    color=MUTED, style="italic")
    return right


def swatch(colour, label, edge: str | None = None):
    return Patch(facecolor=colour, edgecolor=edge or "none", linewidth=0.5 if edge else 0, label=label)


def outline_handle(label, colour=INK, width=1.1, style="-"):
    return Line2D([], [], color=colour, linewidth=width, linestyle=style, label=label)


def point_handle(label, marker="o", colour=INK, size=5.5, edge=PAPER):
    return Line2D([], [], marker=marker, linestyle="", markerfacecolor=colour, markeredgecolor=edge,
                  markeredgewidth=0.6, markersize=size, label=label)


def class_cmap(colours, bounds):
    cmap = ListedColormap(colours)
    return cmap, BoundaryNorm(bounds, cmap.N)


def save(fig, path: Path, png_dpi: int = 200):
    path = Path(path)
    fig.savefig(path, dpi=400)  # rasterized layers at 400 dpi; vectors stay vectors
    fig.savefig(path.with_suffix(".png"), dpi=png_dpi)
    plt.close(fig)
    print(f"wrote {path}")
