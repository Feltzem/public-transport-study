"""Download Geofabrik extracts and clip them to the curated city envelopes."""

from __future__ import annotations

import argparse
import certifi
import json
from pathlib import Path
import shutil
import ssl
import subprocess
from urllib.request import Request, urlopen

import geopandas as gpd


SOURCES = {
    "oslo": "https://download.geofabrik.de/europe/norway-latest.osm.pbf",
    "stockholm": "https://download.geofabrik.de/europe/sweden-latest.osm.pbf",
    "copenhagen": "https://download.geofabrik.de/europe/denmark-latest.osm.pbf",
    "helsinki": "https://download.geofabrik.de/europe/finland-latest.osm.pbf",
    "brussels": "https://download.geofabrik.de/europe/belgium-latest.osm.pbf",
    "paris": "https://download.geofabrik.de/europe/france/ile-de-france-latest.osm.pbf",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare city OSM PBF files for r5py.")
    parser.add_argument("--cities", default="all", help="all or comma-separated city slugs")
    parser.add_argument("--config", type=Path, default=Path("city_config.example.json"))
    parser.add_argument("--source-cache", type=Path, default=Path("./cache/osm_pbf_sources"))
    parser.add_argument("--output-dir", type=Path, default=Path("./data/osm"))
    parser.add_argument("--buffer-degrees", type=float, default=0.05)
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def download(url: str, destination: Path, *, force: bool) -> Path:
    if destination.exists() and destination.stat().st_size > 0 and not force:
        return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    request = Request(url, headers={"User-Agent": "pt-study/1.0"})
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    with urlopen(request, timeout=180, context=ssl_context) as response, temporary.open("wb") as stream:
        while chunk := response.read(4 * 1024 * 1024):
            stream.write(chunk)
    temporary.replace(destination)
    return destination


def main() -> None:
    args = parse_args()
    osmium = shutil.which("osmium")
    if osmium is None:
        raise RuntimeError("osmium is required; update the Conda environment from environment.yml")
    # osmium-tool 1.19 on Windows parses an uppercase ``osmium.EXE`` argv[0]
    # as a subcommand. Conda's path lookup returns that spelling, while the
    # equivalent lowercase executable name works normally.
    osmium = str(Path(osmium).with_name(Path(osmium).name.lower()))
    document = json.loads(args.config.read_text(encoding="utf-8"))
    by_slug = {settings["city_slug"]: name for name, settings in document["cities"].items()}
    selected = list(SOURCES) if args.cities == "all" else [item.strip() for item in args.cities.split(",")]
    boundaries_path = Path(next(iter(document["cities"].values())).get(
        "boundaries_path", "D:/Public Transport Study/Input Data/Boundaries/Boundaries.gpkg"
    ))
    boundaries = gpd.read_file(boundaries_path, layer="cities_study").to_crs("EPSG:4326")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for slug in selected:
        city_name = by_slug[slug]
        boundary = boundaries[boundaries["Name"] == city_name]
        if boundary.empty:
            raise ValueError(f"Boundary not found for {city_name}")
        west, south, east, north = boundary.total_bounds
        bbox = f"{west-args.buffer_degrees},{south-args.buffer_degrees},{east+args.buffer_degrees},{north+args.buffer_degrees}"
        url = SOURCES[slug]
        source_name = url.rsplit("/", 1)[-1]
        source = download(url, args.source_cache / source_name, force=args.force)
        output = args.output_dir / f"{slug}.osm.pbf"
        if output.exists() and output.stat().st_size > 0 and not args.force:
            print(f"Reusing {output}")
            continue
        command = [
            osmium,
            "extract",
            "--bbox",
            bbox,
            "--strategy",
            "complete_ways",
            "--output",
            str(output),
            str(source),
        ]
        if args.force:
            command.insert(-1, "--overwrite")
        subprocess.run(command, check=True)
        print(f"Prepared {output}")


if __name__ == "__main__":
    main()
