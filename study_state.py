"""Stage run-state records shared by run_study.py and the stage CLIs.

A stage is current only when its config, the analysis code, and the output
of the stage it depends on are unchanged, so a re-run urban area invalidates
the transport and travel-time stages built on it.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess


CODE_ROOT = Path(__file__).parent
PROVENANCE_DIR = Path("data/provenance")
STAGES = ["urban", "transport", "gtfs"]
UPSTREAM_OUTPUT = {
    "transport": ("{slug}_growing_refined_validation.json", "summary"),
    "gtfs": ("{slug}_transport_validation.json", "coverage"),
}


def code_version() -> str:
    digest = hashlib.sha256()
    # Only code that shapes analytical outputs; publishing and figure scripts
    # can change without invalidating completed stages.
    stage_scripts = ["calc_urban_area.py", "calc_transport.py", "calc_gtfs.py", "run_study.py", "study_state.py"]
    paths = [*(CODE_ROOT / name for name in stage_scripts), *CODE_ROOT.glob("urban_area/*.py"), *CODE_ROOT.glob("transport/*.py")]
    for path in sorted(paths):
        digest.update(path.relative_to(CODE_ROOT).as_posix().encode())
        digest.update(path.read_bytes())
    try:
        head = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL, cwd=CODE_ROOT
        ).strip()
    except Exception:
        head = "no-git"
    return f"{head}:{digest.hexdigest()[:16]}"


def upstream_fingerprint(slug: str, stage: str) -> str | None:
    """Hash of the upstream stage summary this stage consumes, if any."""
    if stage not in UPSTREAM_OUTPUT:
        return None
    filename, key = UPSTREAM_OUTPUT[stage]
    path = PROVENANCE_DIR / filename.format(slug=slug)
    if not path.exists():
        return "missing"
    payload = json.loads(path.read_text(encoding="utf-8")).get(key, {})
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def config_hash(city_payload: dict, stage: str, slug: str) -> str:
    body = json.dumps(
        {"stage": stage, "config": city_payload, "upstream": upstream_fingerprint(slug, stage)},
        sort_keys=True,
    ).encode()
    return hashlib.sha256(body).hexdigest()


def state_path(slug: str, stage: str) -> Path:
    return PROVENANCE_DIR / f"{slug}_{stage}_run_state.json"


def is_current(slug: str, stage: str, city_payload: dict, version: str) -> bool:
    path = state_path(slug, stage)
    if not path.exists():
        return False
    state = json.loads(path.read_text(encoding="utf-8"))
    return bool(
        state.get("complete")
        and state.get("config_hash") == config_hash(city_payload, stage, slug)
        and state.get("code_version") == version
    )


def write_state(slug: str, stage: str, city_payload: dict, version: str, elapsed_s: float) -> None:
    path = state_path(slug, stage)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(
            {
                "complete": True,
                "completed_at_utc": datetime.now(timezone.utc).isoformat(),
                "config_hash": config_hash(city_payload, stage, slug),
                "upstream_fingerprint": upstream_fingerprint(slug, stage),
                "code_version": version,
                "elapsed_seconds": elapsed_s,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    temporary.replace(path)


def city_payload(config_path: Path, city_name: str) -> dict:
    return json.loads(Path(config_path).read_text(encoding="utf-8"))["cities"][city_name]
