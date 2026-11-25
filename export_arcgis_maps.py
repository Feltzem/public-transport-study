"""Duplicate ArcGIS Pro templates per city and export 300-DPI PDFs.

Run this file with the ArcGIS Pro Python environment after creating layouts
named ``Urban Area Template`` and ``Public Transport Template`` whose map
frames point at maps with the same names.
"""

from __future__ import annotations

import argparse
from pathlib import Path


CITIES = ["oslo", "stockholm", "copenhagen", "helsinki", "brussels", "paris"]
THEMES = {
    "urban": "Urban Area Template",
    "transport": "Public Transport Template",
}


def parse_args():
    parser = argparse.ArgumentParser(description="Export the 12 per-city ArcGIS layouts.")
    parser.add_argument("--project", type=Path, default=Path("../Public Transport Study/Public Transport Study.aprx"))
    parser.add_argument("--gpkg", type=Path, default=Path("../Output/pt_study.gpkg"))
    parser.add_argument("--output-dir", type=Path, default=Path("../Output/Maps"))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        import arcpy
    except ImportError as exc:
        raise RuntimeError("Run this script from the ArcGIS Pro Python environment") from exc
    if not args.gpkg.exists():
        raise FileNotFoundError(f"Published GeoPackage not found: {args.gpkg}")
    project = arcpy.mp.ArcGISProject(str(args.project))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for theme, template_name in THEMES.items():
        template_maps = project.listMaps(template_name)
        template_layouts = project.listLayouts(template_name)
        if not template_maps or not template_layouts:
            raise RuntimeError(f"Create a map and layout named {template_name!r} before running this exporter")
        for slug in CITIES:
            map_name = f"{slug}_{theme}"
            layout_name = f"{slug}_{theme}"
            maps = project.listMaps(map_name)
            city_map = maps[0] if maps else project.copyItem(template_maps[0], map_name)
            layouts = project.listLayouts(layout_name)
            city_layout = layouts[0] if layouts else project.copyItem(template_layouts[0], layout_name)
            for map_frame in city_layout.listElements("MAPFRAME_ELEMENT"):
                map_frame.map = city_map
            for layer in city_map.listLayers():
                if layer.isFeatureLayer and layer.supports("DEFINITIONQUERY"):
                    layer.definitionQuery = f"city_slug = '{slug}'"
            city_layout.exportToPDF(str(args.output_dir / f"{slug}_{theme}.pdf"), resolution=300)
    comparison = project.listLayouts("Comparison")
    if comparison:
        comparison[0].exportToPDF(str(args.output_dir / "comparison.pdf"), resolution=300)
    project.save()
    print(f"Exported layouts to {args.output_dir}")


if __name__ == "__main__":
    main()
