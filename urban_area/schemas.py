"""Schema and data-contract validation helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

PROJECTED_FORBIDDEN_METRIC_CRS = {"EPSG:4326", "EPSG:3857"}
POLYGON_TYPES = {"Polygon", "MultiPolygon"}
POINT_TYPES = {"Point", "MultiPoint"}
LINE_TYPES = {"LineString", "MultiLineString"}


@dataclass
class ContractReport:
    name: str
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    info: dict[str, object] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors

    def add_error(self, message: str) -> None:
        self.errors.append(message)

    def add_warning(self, message: str) -> None:
        self.warnings.append(message)

    def raise_for_errors(self) -> None:
        if self.errors:
            joined = "\n".join(f"- {e}" for e in self.errors)
            raise ValueError(f"{self.name} failed validation:\n{joined}")

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "ok": self.ok,
            "errors": self.errors,
            "warnings": self.warnings,
            "info": self.info,
        }


def _crs_string(gdf) -> str | None:
    if gdf.crs is None:
        return None
    return gdf.crs.to_string() if hasattr(gdf.crs, "to_string") else str(gdf.crs)


def geometry_type_counts(gdf) -> dict[str, int]:
    if gdf.empty:
        return {}
    return {str(k): int(v) for k, v in gdf.geom_type.value_counts(dropna=False).to_dict().items()}


def validate_required_columns(gdf, required_columns: Iterable[str], report: ContractReport) -> None:
    missing = [c for c in required_columns if c not in gdf.columns]
    if missing:
        report.add_error(f"Missing required columns: {', '.join(missing)}")


def validate_metric_crs(gdf, report: ContractReport, allow_web_mercator: bool = False) -> None:
    crs = _crs_string(gdf)
    report.info["crs"] = crs
    if crs is None:
        report.add_error("CRS is missing")
        return
    forbidden = {"EPSG:4326"} if allow_web_mercator else PROJECTED_FORBIDDEN_METRIC_CRS
    if crs.upper() in forbidden:
        report.add_error(f"Metric calculations must not run in {crs}; reproject to the city metric CRS first")


def validate_geometry_types(gdf, allowed_types: set[str], report: ContractReport, require_non_empty: bool = True) -> None:
    counts = geometry_type_counts(gdf)
    report.info["geometry_type_counts"] = counts
    if require_non_empty and gdf.empty:
        report.add_error("GeoDataFrame is empty")
        return
    bad_types = sorted(t for t in counts if t not in allowed_types)
    if bad_types:
        report.add_error(f"Unexpected geometry types: {', '.join(bad_types)}; allowed: {', '.join(sorted(allowed_types))}")


def validate_population_cells(gdf, metric_crs_required: bool = True) -> ContractReport:
    report = ContractReport("population_cells")
    validate_required_columns(gdf, ["population", "geometry"], report)
    validate_geometry_types(gdf, POLYGON_TYPES, report)
    if metric_crs_required:
        validate_metric_crs(gdf, report)
    if "population" in gdf.columns:
        if gdf["population"].isna().any():
            report.add_error("Population contains null values")
        if (gdf["population"] < 0).any():
            report.add_error("Population contains negative values")
        report.info["total_population"] = float(gdf["population"].sum())
    return report


def validate_buildings(gdf, expected_mode: str = "auto") -> ContractReport:
    report = ContractReport("buildings")
    validate_required_columns(gdf, ["geometry"], report)
    counts = geometry_type_counts(gdf)
    report.info["geometry_type_counts"] = counts
    has_polygons = any(t in POLYGON_TYPES for t in counts)
    has_points = any(t in POINT_TYPES for t in counts)
    if expected_mode == "required_footprint" and not has_polygons:
        report.add_error("Building footprints are required, but no Polygon/MultiPolygon geometries are present")
    elif expected_mode == "footprint" and not has_polygons:
        report.add_error("Configured building_mode='footprint', but no Polygon/MultiPolygon geometries are present")
    elif expected_mode == "point" and not has_points:
        report.add_error("Configured building_mode='point', but no Point/MultiPoint geometries are present")
    elif expected_mode == "auto" and not (has_polygons or has_points):
        report.add_error("Buildings contain neither footprint polygons nor point geometries")
    if has_points and not has_polygons:
        report.add_warning(
            "Buildings are point geometries; footprint coverage is unavailable and scoring will use lower-confidence point density/count"
        )
    if has_polygons and has_points:
        report.add_warning("Buildings contain both polygons and points; polygon footprint coverage will be preferred")
    return report


def validate_roads(gdf) -> ContractReport:
    report = ContractReport("roads")
    validate_required_columns(gdf, ["geometry"], report)
    validate_geometry_types(gdf, LINE_TYPES, report, require_non_empty=False)
    return report


def validate_analysis_cells(gdf) -> ContractReport:
    report = ContractReport("analysis_cells")
    required = [
        "cell_id",
        "population",
        "population_density",
        "cell_area_sqm",
        "built_up_coverage",
        "total_road_length_m",
        "road_density_km_per_sqkm",
        "local_road_density_km_per_sqkm",
        "building_signal_norm",
        "building_signal_type",
        "urban_score_base",
        "geometry",
    ]
    validate_required_columns(gdf, required, report)
    validate_geometry_types(gdf, POLYGON_TYPES, report)
    validate_metric_crs(gdf, report)
    if "h3" in gdf.columns and (gdf["h3"].astype(str) == "0").any():
        report.add_warning("Some analysis cells have h3='0'; this should only occur for documented refined subcells")
    return report
