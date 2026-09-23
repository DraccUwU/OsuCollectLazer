"""Settings and paths for OsuCollectLazer."""
from __future__ import annotations

import json
import os
from pathlib import Path

APP_NAME = "OsuCollectLazer"


def app_dir() -> Path:
    """Per-user app data dir (settings, logs)."""
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) if base else Path.home() / ".local" / "share"
    d = root / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


SETTINGS_PATH = app_dir() / "settings.json"
DEFAULT_DOWNLOAD_DIR = Path.home() / "Documents" / "OsuCollectLazer" / "collections"

DEFAULTS: dict = {
    "download_dir": str(DEFAULT_DOWNLOAD_DIR),
    "concurrency": 10,
    "no_video": True,
    "verify_zips": True,
    "lazer_exe": "",
    # what happens after a download finishes:
    #   "auto"   = import the maps (and delete them) as they land, then write the
    #              collection into lazer's database — no clicks anywhere (default)
    #   "manual" = download only; use the "import now" button in the Library
    "pipeline_mode": "auto",
    # maps go straight into lazer's files + realm with the helper, which needs the game
    # closed — closing it for you is part of "one click", so it happens unless disabled
    "close_lazer_before_import": True,
    # import each wave while the download is still running instead of after it
    "stream_import": True,
    # maps per helper run (bigger = more parallelism, bigger peak disk usage)
    "import_chunk": 250,
    # where the collection entry goes: "database" (helper writes it) | "off"
    "collection_mode": "database",
    "mirrors": [
        "nerinyan",
        "beatconnect",
        "catboy",
        "osu.direct",
        "sayobot",
        "nekoha",
        "osudl",
        "hinamizawa",
    ],
    "collection_prefix": "",
    "port": 8765,
}


# keys from older versions that no longer exist; dropped on load/save so a settings file
# from a previous install can't resurrect behaviour the app doesn't have any more
LEGACY_KEYS = (
    "map_import_mode",
    "import_transport",
    "push_batch_size",
    "push_parallel",
    "delete_maps_after_import",
    "import_confirm_timeout",
    "auto_start_lazer",
    "auto_push_maps",
    "extract_workers",
)


def load_settings() -> dict:
    settings = dict(DEFAULTS)
    if SETTINGS_PATH.exists():
        try:
            raw = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        except Exception:
            raw = {}
        settings.update({k: v for k, v in raw.items() if k in DEFAULTS})
        # "wizard" was a pipeline mode for the game's import screen; imports are direct now
        if raw.get("pipeline_mode") == "wizard":
            settings["pipeline_mode"] = "manual"
    return settings


def save_settings(settings: dict) -> None:
    merged = dict(DEFAULTS)
    merged.update({k: v for k, v in settings.items() if k in DEFAULTS or k.startswith("_")})
    SETTINGS_PATH.write_text(json.dumps(merged, indent=2), encoding="utf-8")


def download_dir(settings: dict | None = None) -> Path:
    s = settings or load_settings()
    d = Path(s["download_dir"]).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    return d
