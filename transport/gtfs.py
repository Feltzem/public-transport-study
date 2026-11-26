"""GTFS download, weekday frequency calculation, and OSM station matching."""

from __future__ import annotations

from datetime import date, timedelta
from difflib import SequenceMatcher
import hashlib
import io
import os
from pathlib import Path
import re
import shutil
import unicodedata
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen
import zipfile

import geopandas as gpd
import numpy as np
import pandas as pd
import shapely

from .config import TransportConfig


# Basic GTFS route types plus the extended (HVT) types used by Entur and
# other European feeds. Coach (200-209), demand-responsive (715) and taxi
# (1500+) services are excluded from the study.
_ROUTE_TYPE_RANGES = [
    (0, 0, "tram"),
    (1, 1, "metro"),
    (2, 2, "rail"),
    (3, 3, "bus"),
    (4, 4, "ferry"),
    (5, 6, "cable"),
    (7, 7, "funicular"),
    (11, 11, "bus"),  # trolleybus
    (12, 12, "metro"),  # monorail
    (100, 117, "rail"),
    (400, 405, "metro"),
    (700, 714, "bus"),
    (716, 716, "bus"),
    (800, 800, "bus"),
    (900, 906, "tram"),
    (1000, 1021, "ferry"),
    (1200, 1200, "ferry"),
    (1300, 1307, "cable"),
    (1400, 1400, "funicular"),
]
RAIL_TIER_MODES = ("rail", "metro", "tram")
OSM_TO_GTFS_MODES = {
    "rail": {"rail"},
    "subway": {"metro"},
    "light_rail": {"metro", "tram"},
    "monorail": {"metro"},
    "tram": {"tram"},
}
ALL_MODES = ("rail", "metro", "tram", "bus", "ferry", "funicular", "cable")


def route_mode(route_type) -> str | None:
    """Return the study mode for a GTFS ``route_type``, or None if excluded."""
    try:
        value = int(route_type)
    except (TypeError, ValueError):
        return None
    for low, high, mode in _ROUTE_TYPE_RANGES:
        if low <= value <= high:
            return mode
    return None


def typical_tuesday(reference: date | None = None) -> date:
    reference = reference or date.today()
    days = (1 - reference.weekday()) % 7
    return reference + timedelta(days=days)


def _ssl_context():
    """Prefer certifi's CA bundle; a malformed Windows certificate store can
    otherwise break every HTTPS download."""
    import ssl

    try:
        import certifi
    except ImportError:
        return ssl.create_default_context()
    return ssl.create_default_context(cafile=certifi.where())


def _expand_env(template: str, what: str) -> str:
    value = os.path.expandvars(template)
    if "$" in value:
        # Report the template, never the expanded value.
        raise RuntimeError(f"{what} has an unresolved environment variable: {template}")
    return value


def feed_request_headers(config: TransportConfig, configured_url: str) -> dict[str, str]:
    """HTTP headers for one feed. Header values in ``gtfs_feed_headers`` are
    environment-variable templates such as ``"${DELIJN_API_KEY}"``, keyed by
    the configured (unexpanded) feed URL, so keys never sit in config files."""
    headers = {"User-Agent": "pt-study/1.0"}
    for name, template in (config.gtfs_feed_headers or {}).get(configured_url, {}).items():
        headers[name] = _expand_env(template, f"Header {name} for GTFS feed {configured_url}")
    return headers


_SECRET_QUERY_KEYS = {"key", "api_key", "apikey", "token", "access_token", "subscription-key", "code"}


def sanitise_feed_url(configured_url: str) -> str:
    """Return the configured feed URL with any literal secret query values masked.

    Environment-variable placeholders are kept, since they hold no secret."""
    parsed = urlparse(configured_url)
    if not parsed.query:
        return configured_url
    parts = []
    for item in parsed.query.split("&"):
        name, sep, value = item.partition("=")
        if sep and name.lower() in _SECRET_QUERY_KEYS and not value.startswith("$"):
            value = "***"
        parts.append(f"{name}{sep}{value}")
    return parsed._replace(query="&".join(parts)).geturl()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(4 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def feed_provenance(config: TransportConfig, feed_paths: list[Path]) -> list[dict]:
    """Per-feed provenance: configured source (sanitised), cached path, size, SHA-256."""
    sources = list(config.gtfs_feed_urls)
    records = []
    for index, path in enumerate(feed_paths):
        path = Path(path)
        records.append(
            {
                "source": sanitise_feed_url(sources[index]) if index < len(sources) else None,
                "path": str(path),
                "bytes": path.stat().st_size,
                "sha256": file_sha256(path),
            }
        )
    return records


def download_gtfs_feeds(config: TransportConfig, *, force: bool = False) -> list[Path]:
    """Download configured feeds atomically, expanding environment variables
    in URLs and in any per-feed request headers."""
    outputs = []
    directory = config.cache_dir / "gtfs" / config.resolved_slug()
    directory.mkdir(parents=True, exist_ok=True)
    for index, configured_url in enumerate(config.gtfs_feed_urls, start=1):
        url = _expand_env(configured_url, "GTFS URL")
        destination = directory / f"feed_{index}.zip"
        if force or not destination.exists() or destination.stat().st_size == 0:
            temporary = destination.with_suffix(".zip.part")
            parsed = urlparse(url)
            local_source = Path(unquote(parsed.path.lstrip("/"))) if parsed.scheme == "file" else Path(url)
            if local_source.exists():
                shutil.copyfile(local_source, temporary)
            else:
                request = Request(url, headers=feed_request_headers(config, configured_url))
                with urlopen(request, timeout=120, context=_ssl_context()) as response, temporary.open("wb") as stream:
                    while chunk := response.read(1024 * 1024):
                        stream.write(chunk)
            temporary.replace(destination)
        outputs.append(destination)
    if not outputs:
        raise ValueError(f"No GTFS feed URLs configured for {config.city_name}")
    return outputs


def _read_csv(archive: zipfile.ZipFile, name: str, **kwargs) -> pd.DataFrame:
    try:
        with archive.open(name) as stream:
            return pd.read_csv(stream, low_memory=False, **kwargs)
    except KeyError:
        return pd.DataFrame()


def _active_services(archive: zipfile.ZipFile, service_date: date) -> set[str]:
    ymd = int(service_date.strftime("%Y%m%d"))
    weekday = service_date.strftime("%A").lower()
    calendar = _read_csv(archive, "calendar.txt", dtype={"service_id": str})
    active: set[str] = set()
    if not calendar.empty:
        dates = calendar.copy()
        dates["start_date"] = pd.to_numeric(dates["start_date"], errors="coerce")
        dates["end_date"] = pd.to_numeric(dates["end_date"], errors="coerce")
        mask = (dates["start_date"] <= ymd) & (dates["end_date"] >= ymd) & (dates.get(weekday, 0) == 1)
        active.update(dates.loc[mask, "service_id"].astype(str))
    exceptions = _read_csv(archive, "calendar_dates.txt", dtype={"service_id": str})
    if not exceptions.empty:
        exceptions["date"] = pd.to_numeric(exceptions["date"], errors="coerce")
        today = exceptions[exceptions["date"] == ymd]
        active.update(today.loc[today["exception_type"] == 1, "service_id"].astype(str))
        active.difference_update(today.loc[today["exception_type"] == 2, "service_id"].astype(str))
    return active


def _service_date_range(archive: zipfile.ZipFile) -> tuple[int | None, int | None]:
    calendar = _read_csv(archive, "calendar.txt", dtype={"service_id": str})
    exceptions = _read_csv(archive, "calendar_dates.txt", dtype={"service_id": str})
    starts, ends = [], []
    if not calendar.empty:
        starts.append(pd.to_numeric(calendar["start_date"], errors="coerce").min())
        ends.append(pd.to_numeric(calendar["end_date"], errors="coerce").max())
    if not exceptions.empty:
        added = exceptions[pd.to_numeric(exceptions["exception_type"], errors="coerce") == 1]
        dates = pd.to_numeric(added["date"], errors="coerce")
        starts.append(dates.min())
        ends.append(dates.max())
    starts = [value for value in starts if pd.notna(value)]
    ends = [value for value in ends if pd.notna(value)]
    return (int(min(starts)) if starts else None, int(max(ends)) if ends else None)


def _time_seconds(value: pd.Series) -> pd.Series:
    parts = value.astype(str).str.split(":", expand=True)
    return (
        pd.to_numeric(parts[0], errors="coerce") * 3600
        + pd.to_numeric(parts[1], errors="coerce") * 60
        + pd.to_numeric(parts[2], errors="coerce")
    )


def _read_active_stop_times(archive: zipfile.ZipFile, trip_ids: set[str]) -> pd.DataFrame:
    """Read stop_times for the given trips in chunks to bound memory."""
    wanted = {"trip_id", "stop_id", "departure_time", "stop_sequence", "pickup_type"}
    frames = []
    with archive.open("stop_times.txt") as stream:
        reader = pd.read_csv(
            stream,
            usecols=lambda column: column in wanted,
            dtype={"trip_id": str, "stop_id": str, "departure_time": str},
            chunksize=2_000_000,
            low_memory=False,
        )
        for chunk in reader:
            frames.append(chunk[chunk["trip_id"].isin(trip_ids)])
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=sorted(wanted))


def _direction_key(trips: pd.DataFrame) -> pd.Series:
    """Per-trip direction key from ``direction_id``; "infer" where it is missing.

    Trips marked "infer" get a direction per stop from the bearing to their
    next stop (see ``_bearing_direction_keys``)."""
    key = pd.Series("infer", index=trips.index, dtype=object)
    if "direction_id" in trips:
        direction = pd.to_numeric(trips["direction_id"], errors="coerce")
        key = ("d:" + direction.astype("Int64").astype(str)).where(direction.notna(), key)
    return key


def _bearing_direction_keys(departures: pd.DataFrame, stops: pd.DataFrame) -> pd.Series:
    """Direction keys for departures whose trips have no ``direction_id``.

    Each departure's bearing to the trip's next stop is folded onto the
    station's principal axis (axial mean of doubled angles, per station and
    mode); departures on either side of the perpendicular form the two
    directions. On a line through a station this reproduces ``direction_id``.
    Departures with no usable next-stop position fall back to the headsign.
    """
    lon = pd.to_numeric(stops.set_index("stop_id")["stop_lon"], errors="coerce")
    lat = pd.to_numeric(stops.set_index("stop_id")["stop_lat"], errors="coerce")
    lat0 = np.radians(departures["stop_id"].map(lat))
    dx = (departures["next_stop_id"].map(lon) - departures["stop_id"].map(lon)) * np.cos(lat0)
    dy = departures["next_stop_id"].map(lat) - departures["stop_id"].map(lat)
    theta = np.arctan2(dy, dx).where((dx.abs() + dy.abs()) > 0)
    work = pd.DataFrame(
        {"station_key": departures["station_key"], "mode": departures["mode"], "s": np.sin(2 * theta), "c": np.cos(2 * theta)}
    )
    sums = work.groupby(["station_key", "mode"])[["s", "c"]].transform("sum")
    axis = 0.5 * np.arctan2(sums["s"], sums["c"])
    side = np.cos(theta - axis) >= 0
    keys = pd.Series(np.where(side, "b:0", "b:1"), index=departures.index, dtype=object)
    headsign = departures.get("trip_headsign", pd.Series(index=departures.index, dtype=object))
    fallback = ("h:" + headsign.astype(str)).where(headsign.notna(), "all")
    return keys.where(theta.notna(), fallback)


def _station_departures(stop_times, trips, stops, start_hour, end_hour, feed_prefix, bbox) -> gpd.GeoDataFrame:
    stop_times = stop_times.copy()
    stop_times["stop_sequence"] = pd.to_numeric(stop_times["stop_sequence"], errors="coerce")
    if (trips["direction_key"] == "infer").any():
        # The next stop of each trip, needed only where direction must be inferred.
        stop_times = stop_times.sort_values(["trip_id", "stop_sequence"], kind="stable")
        stop_times["next_stop_id"] = stop_times.groupby("trip_id")["stop_id"].shift(-1)
    last_sequence = stop_times.groupby("trip_id")["stop_sequence"].transform("max")
    departures = stop_times[stop_times["stop_sequence"] < last_sequence]
    if "pickup_type" in departures:
        departures = departures[pd.to_numeric(departures["pickup_type"], errors="coerce").fillna(0) != 1]
    trip_columns = ["trip_id", "mode", "direction_key"] + (["trip_headsign"] if "trip_headsign" in trips else [])
    departures = departures.merge(trips[trip_columns], on="trip_id", how="inner")

    # Some feeds (HSL) pad empty parent_station values with a space.
    parent = stops.get("parent_station", pd.Series(index=stops.index, dtype=object)).fillna("").astype(str).str.strip()
    station_of = dict(zip(stops["stop_id"], parent.where(parent.str.len() > 0, stops["stop_id"])))
    departures["station_key"] = departures["stop_id"].map(station_of).fillna(departures["stop_id"])
    departures["departure_seconds"] = _time_seconds(departures["departure_time"])
    infer = departures["direction_key"] == "infer"
    if infer.any():
        departures.loc[infer, "direction_key"] = _bearing_direction_keys(departures[infer], stops)

    keys = ["station_key", "mode"]
    day = departures.groupby(keys).size().rename("departures_day")
    hours = max(1, end_hour - start_hour)
    in_window = departures[
        (departures["departure_seconds"] >= start_hour * 3600) & (departures["departure_seconds"] < end_hour * 3600)
    ]
    by_direction = in_window.groupby(keys + ["direction_key"]).size()
    window = pd.DataFrame(
        {
            "departures_per_hr": by_direction.groupby(level=keys).max() / hours,
            "departures_per_hr_total": by_direction.groupby(level=keys).sum() / hours,
        }
    )
    stations = day.to_frame().join(window, how="left").fillna({"departures_per_hr": 0.0, "departures_per_hr_total": 0.0})
    stations = stations.reset_index()

    stop_lookup = stops.set_index("stop_id")
    first_child = departures.drop_duplicates("station_key").set_index("station_key")["stop_id"]
    location_id = stations["station_key"].where(
        stations["station_key"].isin(stop_lookup.index), stations["station_key"].map(first_child)
    )
    stations["stop_name"] = location_id.map(stop_lookup.get("stop_name", pd.Series(dtype=object)))
    stations["stop_lon"] = pd.to_numeric(location_id.map(stop_lookup["stop_lon"]), errors="coerce")
    stations["stop_lat"] = pd.to_numeric(location_id.map(stop_lookup["stop_lat"]), errors="coerce")
    stations = stations[stations["stop_lon"].notna() & stations["stop_lat"].notna()].copy()
    if bbox is not None:
        min_lon, min_lat, max_lon, max_lat = bbox
        stations = stations[
            stations["stop_lon"].between(min_lon, max_lon) & stations["stop_lat"].between(min_lat, max_lat)
        ].copy()
    stations["stop_id"] = feed_prefix + ":" + stations["station_key"].astype(str)
    geometry = gpd.points_from_xy(stations["stop_lon"], stations["stop_lat"])
    columns = ["stop_id", "stop_name", "mode", "departures_per_hr", "departures_per_hr_total", "departures_day"]
    return gpd.GeoDataFrame(stations[columns].reset_index(drop=True), geometry=geometry, crs="EPSG:4326")


def parse_feed_departures(
    path: Path,
    *,
    service_date: date,
    start_hour: int = 7,
    end_hour: int = 9,
    feed_prefix: str = "feed",
    modes: tuple[str, ...] = ALL_MODES,
    bbox: tuple[float, float, float, float] | None = None,
) -> gpd.GeoDataFrame:
    """Return station-level departures for one GTFS archive and service date.

    One row is returned per (station, mode) served on ``service_date``.
    Platforms roll up to their ``parent_station``. ``departures_per_hr`` is
    the busiest direction in the window; ``departures_per_hr_total`` sums
    all directions. A trip's final stop and no-pickup stops are not
    departures. ``bbox`` (lon/lat) limits the stations returned.
    """
    with zipfile.ZipFile(path) as archive:
        first, last = _service_date_range(archive)
        ymd = int(service_date.strftime("%Y%m%d"))
        if first is not None and last is not None and not first <= ymd <= last:
            raise ValueError(f"Service date {service_date} is outside the feed calendar {first}-{last}: {path}")
        active_services = _active_services(archive, service_date)
        if not active_services:
            raise ValueError(f"No services are active on {service_date} in {path}")
        routes = _read_csv(archive, "routes.txt", dtype={"route_id": str})
        trips = _read_csv(archive, "trips.txt", dtype={"route_id": str, "trip_id": str, "service_id": str})
        stops = _read_csv(archive, "stops.txt", dtype={"stop_id": str, "parent_station": str})
        if routes.empty or "route_type" not in routes or trips.empty or stops.empty:
            raise ValueError(f"Incomplete GTFS feed: {path}")
        routes["mode"] = routes["route_type"].map(route_mode)
        routes = routes[routes["mode"].isin(modes)]
        trips = trips[trips["route_id"].isin(set(routes["route_id"])) & trips["service_id"].isin(active_services)].copy()
        trips["mode"] = trips["route_id"].map(routes.set_index("route_id")["mode"])
        trips["direction_key"] = _direction_key(trips)
        stop_times = _read_active_stop_times(archive, set(trips["trip_id"]))
    if stop_times.empty:
        raise ValueError(f"No {', '.join(modes)} departures on {service_date} in {path}")
    return _station_departures(stop_times, trips, stops, start_hour, end_hour, feed_prefix, bbox)


def load_departures(
    feed_paths: list[Path],
    *,
    service_date: date,
    start_hour: int = 7,
    end_hour: int = 9,
    modes: tuple[str, ...] = ALL_MODES,
    bbox: tuple[float, float, float, float] | None = None,
) -> gpd.GeoDataFrame:
    frames = [
        parse_feed_departures(
            path,
            service_date=service_date,
            start_hour=start_hour,
            end_hour=end_hour,
            feed_prefix=f"feed{index}",
            modes=modes,
            bbox=bbox,
        )
        for index, path in enumerate(feed_paths, start=1)
    ]
    return gpd.GeoDataFrame(pd.concat(frames, ignore_index=True), geometry="geometry", crs="EPSG:4326")


def _normalise_name(value) -> str:
    if value is None or pd.isna(value):
        return ""
    text = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def consolidate_stations(
    stations: gpd.GeoDataFrame,
    *,
    metric_crs: str,
    merge_distance_m: float = 150.0,
) -> gpd.GeoDataFrame:
    """Merge same-mode GTFS stations sharing a normalised name within a distance.

    Feeds sometimes split one physical station into several stop places; the
    merged station keeps the busiest-direction frequency of its members and
    sums their totals.
    """
    work = stations.to_crs(metric_crs).reset_index(drop=True).copy()
    if work.empty:
        work["station_id"] = pd.Series(dtype=int)
        work["gtfs_station_ids"] = pd.Series(dtype=object)
        return work
    work["_name_norm"] = work["stop_name"].apply(_normalise_name)
    buffered = work[["geometry"]].copy()
    buffered["geometry"] = work.geometry.buffer(merge_distance_m / 2)
    pairs = gpd.sjoin(buffered, buffered, how="inner", predicate="intersects")
    left = pairs.index.to_numpy()
    right = pairs["index_right"].to_numpy()
    same = (
        (left < right)
        & (work["mode"].to_numpy()[left] == work["mode"].to_numpy()[right])
        & (work["_name_norm"].to_numpy()[left] == work["_name_norm"].to_numpy()[right])
        & (work["_name_norm"].to_numpy()[left] != "")
    )
    parent = list(range(len(work)))

    def find(item):
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = parent[item]
        return item

    for a, b in zip(left[same], right[same]):
        root_a, root_b = find(a), find(b)
        if root_a != root_b:
            parent[root_b] = root_a
    work["_group"] = [find(item) for item in range(len(work))]
    grouped = work.groupby("_group")
    merged = gpd.GeoDataFrame(
        {
            "name": grouped["stop_name"].first(),
            "mode": grouped["mode"].first(),
            "gtfs_station_ids": grouped["stop_id"].agg(lambda ids: ";".join(sorted(ids))),
            "departures_per_hr": grouped["departures_per_hr"].max(),
            "departures_per_hr_total": grouped["departures_per_hr_total"].sum(),
            "departures_day": grouped["departures_day"].sum(),
            "geometry": grouped["geometry"].agg(lambda points: shapely.MultiPoint(list(points)).centroid),
        },
        geometry="geometry",
        crs=work.crs,
    ).reset_index(drop=True)
    merged.insert(0, "station_id", range(len(merged)))
    return merged


def match_gtfs_to_osm_stations(
    osm_stations: gpd.GeoDataFrame,
    gtfs_stops: gpd.GeoDataFrame,
    *,
    metric_crs: str,
    tolerance_m: float = 500.0,
    min_name_similarity: float = 0.45,
) -> tuple[gpd.GeoDataFrame, pd.DataFrame]:
    """Match OSM stations to GTFS stations of a compatible mode.

    Used only when ``station_source='osm'``. Candidates must serve a mode
    compatible with the OSM station and have departures on the service date,
    so a rail station cannot pick up a same-named bus stop place.
    """
    match_columns = ["gtfs_stop_id", "gtfs_stop_name", "match_distance_m", "name_similarity", "departures_per_hr", "departures_per_hr_total"]
    # Drop columns from any earlier match so a re-run merges cleanly.
    stations = osm_stations.to_crs(metric_crs).drop(columns=match_columns, errors="ignore").copy()
    stops = gtfs_stops.to_crs(metric_crs).copy()
    stops = stops[stops["departures_day"] > 0].reset_index(drop=True)
    stops["_name_norm"] = stops.get("stop_name", pd.Series(index=stops.index, dtype=object)).apply(_normalise_name)
    rows = []
    for _, station in stations.iterrows():
        candidates = stops.iloc[list(stops.sindex.query(station.geometry.buffer(tolerance_m), predicate="intersects"))].copy()
        compatible = OSM_TO_GTFS_MODES.get(str(station.get("mode")), set(RAIL_TIER_MODES))
        candidates = candidates[candidates["mode"].isin(compatible)]
        station_name = _normalise_name(station.get("name"))
        best = None
        for _, candidate in candidates.iterrows():
            distance = float(station.geometry.distance(candidate.geometry))
            similarity = SequenceMatcher(None, station_name, candidate["_name_norm"]).ratio() if station_name else 1.0
            if station_name and similarity < min_name_similarity:
                continue
            score = similarity - 0.2 * distance / tolerance_m
            if best is None or score > best["score"]:
                best = {
                    "station_id": station["station_id"],
                    "gtfs_stop_id": candidate["stop_id"],
                    "gtfs_stop_name": candidate.get("stop_name"),
                    "match_distance_m": distance,
                    "name_similarity": similarity,
                    "departures_per_hr": float(candidate["departures_per_hr"]),
                    "departures_per_hr_total": float(candidate["departures_per_hr_total"]),
                    "score": score,
                }
        if best is not None:
            rows.append(best)
    diagnostics = pd.DataFrame(rows, columns=[
        "station_id", "gtfs_stop_id", "gtfs_stop_name", "match_distance_m",
        "name_similarity", "departures_per_hr", "departures_per_hr_total", "score",
    ])
    result = stations.merge(diagnostics.drop(columns="score", errors="ignore"), on="station_id", how="left")
    for column in ["departures_per_hr", "departures_per_hr_total"]:
        result[column] = result[column].fillna(0.0) if column in result else 0.0
    return result, diagnostics


def _hours(values: pd.Series) -> pd.Series:
    return pd.to_numeric(values.astype(str).str.split(":", n=1).str[0], errors="coerce")


def r5_feed_path(feed_path: Path, city_slug: str) -> Path:
    """Path for the clipped R5 copy of ``feed_path``, unique per city and feed content.

    r5py copies every input into its own cache directory under the file's base
    name and reuses an existing copy of that name. Two cities' clipped feeds
    must therefore never share a name, and a changed feed must get a new one.
    """
    digest = file_sha256(feed_path)[:12]
    return feed_path.with_name(f"{city_slug}_{feed_path.stem}_{digest}_r5.zip")


def prepare_r5_feed(
    feed_path: Path,
    output_path: Path,
    *,
    bbox: tuple[float, float, float, float],
    max_hours: int = 72,
) -> Path:
    """Write a city-clipped GTFS copy that R5 can load.

    National feeds can contain multi-day trips (times beyond R5's 72-hour
    limit) and incomplete optional files that make R5 reject the whole feed.
    The copy keeps stop_times at stops inside ``bbox`` (lon/lat), drops trips
    with any time beyond ``max_hours``, and omits the optional transfers and
    shapes files; R5 computes walking transfers itself.
    """
    if output_path.exists() and output_path.stat().st_mtime >= feed_path.stat().st_mtime:
        return output_path
    min_lon, min_lat, max_lon, max_lat = bbox
    with zipfile.ZipFile(feed_path) as archive:
        stops = _read_csv(archive, "stops.txt", dtype=str)
        lon = pd.to_numeric(stops["stop_lon"], errors="coerce")
        lat = pd.to_numeric(stops["stop_lat"], errors="coerce")
        local = set(stops.loc[lon.between(min_lon, max_lon) & lat.between(min_lat, max_lat), "stop_id"])
        kept_rows, bad_trips = [], set()
        with archive.open("stop_times.txt") as stream:
            for chunk in pd.read_csv(stream, dtype=str, chunksize=2_000_000, keep_default_na=False):
                hours = pd.concat([_hours(chunk["arrival_time"]), _hours(chunk["departure_time"])], axis=1).max(axis=1)
                bad_trips.update(chunk.loc[hours >= max_hours, "trip_id"])
                kept_rows.append(chunk[chunk["stop_id"].isin(local)])
        stop_times = pd.concat(kept_rows, ignore_index=True)
        stop_times = stop_times[~stop_times["trip_id"].isin(bad_trips)]
        trip_counts = stop_times.groupby("trip_id").size()
        stop_times = stop_times[stop_times["trip_id"].isin(trip_counts[trip_counts >= 2].index)]
        trips = _read_csv(archive, "trips.txt", dtype=str, keep_default_na=False)
        trips = trips[trips["trip_id"].isin(set(stop_times["trip_id"]))].drop(columns=["shape_id"], errors="ignore")
        routes = _read_csv(archive, "routes.txt", dtype=str, keep_default_na=False)
        routes = routes[routes["route_id"].isin(set(trips["route_id"]))]
        used_stops = set(stop_times["stop_id"])
        if "parent_station" in stops:
            stops["parent_station"] = stops["parent_station"].fillna("").astype(str).str.strip()
            parents = set(stops.loc[stops["stop_id"].isin(used_stops), "parent_station"]) - {""}
        else:
            parents = set()
        stops = stops[stops["stop_id"].isin(used_stops | parents)].fillna("")
        services = set(trips["service_id"])
        tables = {"stops.txt": stops, "stop_times.txt": stop_times, "trips.txt": trips, "routes.txt": routes}
        for name in ["calendar.txt", "calendar_dates.txt"]:
            table = _read_csv(archive, name, dtype=str, keep_default_na=False)
            if not table.empty:
                tables[name] = table[table["service_id"].isin(services)]
        for name in ["agency.txt", "feed_info.txt"]:
            table = _read_csv(archive, name, dtype=str, keep_default_na=False)
            if not table.empty:
                tables[name] = table
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(".zip.part")
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as sink:
        for name, table in tables.items():
            sink.writestr(name, table.to_csv(index=False))
    temporary.replace(output_path)
    return output_path
