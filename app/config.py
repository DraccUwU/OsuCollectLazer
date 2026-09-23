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
    #   "auto"   = import maps into the running game as one batch task, delete them once
    #              lazer confirms the import, then write the collection into lazer's
    #              database — no clicks anywhere (default)
    #   "wizard" = stage maps + collection.db for lazer's own import screen (manual)
    #   "manual" = download only; use the Library buttons
    "pipeline_mode": "auto",
    # where the collection entry goes: "database" (no in-game steps) | "wizard" | "off"
    "collection_mode": "database",
    # free the .osz files as soon as lazer confirms the import (they are huge)
    "delete_maps_after_import": True,
    "import_chunk": 250,
    "import_confirm_timeout": 1800,
    # how the maps are handed to lazer: paths per `osu!.exe` launch, and how many
    # launches run at once (the forwarder's cold start is the bottleneck, not the game)
    "push_batch_size": 20,
    "push_parallel": 8,
    # how the maps reach the game:
    #   "auto"     = write straight to lazer's IPC pipe, fall back to the launcher
    #   "pipe"     = only the pipe (fails loudly if unavailable)
    #   "launcher" = only `osu!.exe <paths…>` (one launcher process per batch)
    #   "direct"   = import into lazer's store + realm with the helper, in parallel
    #                (~8x the game's serial import, needs osu!lazer closed)
    "import_transport": "auto",
    # hand maps to the game while they are still downloading: lazer imports serially
    # (~1 map/s), so overlapping that with the transfer is free wall-clock time
    "stream_import": True,
    # starting osu!lazer takes over the screen; allow turning the auto-launch off
    "auto_start_lazer": True,
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
    "auto_push_maps": True,
    "push_batch_size": 20,
    "collection_prefix": "",
    "port": 8765,
}


def load_settings() -> dict:
    settings = dict(DEFAULTS)
    raw: dict = {}
    if SETTINGS_PATH.exists():
        try:
            raw = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            settings.update(raw)
        except Exception:
            raw = {}
    # migrate the old map_import_mode key
    legacy = raw.get("map_import_mode")
    if legacy and "pipeline_mode" not in raw:
        settings["pipeline_mode"] = {"push": "auto", "batch": "wizard"}.get(legacy, "auto")
    settings.setdefault("extract_workers", 4)
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
