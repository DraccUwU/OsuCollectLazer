"""Watch osu!lazer's database log to confirm that imports actually happened.

Why this is needed before deleting anything: the IPC forward and window drops only
hand files *to* the game — the import itself is asynchronous. Deleting the .osz
files before lazer has read them would lose the maps, so the app waits for lazer's
own log to report the import, then frees the space.

Lines that count as "this set is now in lazer":
    [xxxxx] Import successfully completed!
    [xxxxx] Found existing beatmap for <name> – skipping import.
Failures look like:
    [xxxxx] No content found in beatmap archive ...
"""
from __future__ import annotations

import re
import time
from pathlib import Path

OK_PATTERNS = (
    re.compile(r"Import successfully completed!"),
    re.compile(r"Found existing beatmap for .*?[–-] skipping import"),
)
FAIL_PATTERNS = (
    re.compile(r"No content found in (?:first \.osu file of )?beatmap archive"),
    re.compile(r"Import failed"),
)
DROP_PATTERN = re.compile(r"Handling batch import of (\d+) files")


def log_dir(data_dir: Path | None = None) -> Path | None:
    from . import lazer

    d = data_dir or lazer.data_dir()
    if not d:
        return None
    candidate = Path(d) / "logs"
    return candidate if candidate.is_dir() else None


def newest_database_log(data_dir: Path | None = None) -> Path | None:
    d = log_dir(data_dir)
    if not d:
        return None
    logs = sorted(d.glob("*.database.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    return logs[0] if logs else None


def position(data_dir: Path | None = None) -> dict:
    """Record the current end of the newest database log before importing."""
    log = newest_database_log(data_dir)
    if not log:
        return {"path": None, "offset": 0, "ok": 0, "failed": 0, "drops": 0, "taken_at": time.time()}
    with open(log, "rb") as fh:
        fh.seek(0, 2)
        offset = fh.tell()
    return {"path": str(log), "offset": offset, "ok": 0, "failed": 0, "drops": 0, "taken_at": time.time()}


def wait_for_log_after(timestamp: float, data_dir: Path | None = None, *, timeout: float = 120.0) -> dict:
    """Position at the end of a log file that is newer than `timestamp`.

    lazer writes a new `<id>.database.log` per run, so a marker taken before the game
    we just launched has produced its log file would watch the *previous* run's file
    and never see a single confirmation.
    """
    deadline = time.monotonic() + timeout
    while True:
        log = newest_database_log(data_dir)
        if log and log.stat().st_mtime > timestamp:
            return position(data_dir)
        if time.monotonic() >= deadline:
            return position(data_dir)
        time.sleep(1.0)


def read_since(marker: dict) -> dict:
    """Count confirmations/failures written after `marker`.

    Follows to a newer log file when the game was (re)started after the marker was
    taken: everything in a file newer than the marker's is by definition new.
    """
    path = marker.get("path")
    if not path:
        return marker
    newest = newest_database_log(Path(path).parent.parent)
    if newest and Path(path) != newest and marker.get("taken_at") is not None:
        try:
            if newest.stat().st_mtime >= float(marker.get("taken_at", 0)):
                marker = {**marker, "path": str(newest), "offset": 0}
                path = str(newest)
        except OSError:
            pass
    log = Path(path)
    try:
        size = log.stat().st_size
    except OSError:
        return marker
    offset = marker.get("offset", 0)
    if size < offset:  # log rotated
        offset = 0
    try:
        with open(log, "rb") as fh:
            fh.seek(offset)
            chunk = fh.read(size - offset)
    except OSError:
        return marker
    text = chunk.decode("utf-8", "replace")
    marker = dict(marker)
    marker["offset"] = offset + len(chunk)
    marker["ok"] = marker.get("ok", 0) + sum(len(p.findall(text)) for p in OK_PATTERNS)
    marker["failed"] = marker.get("failed", 0) + sum(len(p.findall(text)) for p in FAIL_PATTERNS)
    marker["drops"] = marker.get("drops", 0) + sum(int(m) for m in DROP_PATTERN.findall(text))
    return marker


def wait_for_imports(expect: int, marker: dict, *, timeout: float = 1800.0, on_tick=None) -> dict:
    """Poll until lazer has confirmed `expect` imports (or `timeout` elapses)."""
    deadline = time.monotonic() + timeout
    state = dict(marker)
    while True:
        state = read_since(state)
        done = state.get("ok", 0) + state.get("failed", 0)
        if on_tick:
            on_tick(state)
        if done >= expect or time.monotonic() >= deadline:
            return state
        time.sleep(2.0)
