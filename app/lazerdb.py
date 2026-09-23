"""Write collections straight into osu!lazer's realm database.

Uses the bundled LazerDb helper (tools/LazerDb), which calls lazer's own
`LegacyCollectionImporter` — the same code the in-game import screen runs — so
collections merge by name and hashes are de-duplicated exactly like lazer does it.

Safety:
  * the helper refuses to open the database unless the installed lazer's realm
    schema version matches the version it was built against (a mismatch could
    upgrade the database beyond what the installed game understands),
  * a timestamped copy of client.realm is taken before any write,
  * the helper reports the collections before and after, and we require the
    expected collection to be present afterwards before reporting success.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from . import config, lazer

HELPER = Path(__file__).resolve().parent.parent / "tools" / "LazerDb" / "bin" / "Release" / "net10.0" / "LazerDb.exe"
BACKUP_KEEP = 5
TIMEOUT = 900


def helper_path() -> Path | None:
    return HELPER if HELPER.is_file() else None


def installed_osu_game_dll() -> Path | None:
    local = Path.home() / "AppData" / "Local"
    for candidate in (
        local / "osulazer" / "current" / "osu.Game.dll",
        local / "osulazer" / "osu.Game.dll",
        local / "osu" / "osu.Game.dll",
    ):
        if candidate.is_file():
            return candidate
    return None


def version_status() -> dict:
    """Compare the helper's realm schema version with the installed lazer build."""
    helper = helper_path()
    dll = installed_osu_game_dll()
    if not helper:
        return {"available": False, "reason": "helper not built (tools/LazerDb)"}
    if not dll:
        return {"available": False, "reason": "could not find the installed osu.Game.dll"}
    try:
        proc = subprocess.run(
            [str(helper), "--versions-for", str(dll)],
            capture_output=True,
            text=True,
            timeout=180,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        payload = json.loads(proc.stdout.strip() or "{}")
        payload["available"] = bool(payload.get("match"))
        if not payload.get("available"):
            payload["reason"] = (
                f"schema mismatch: tool {payload.get('tool_schema_version')} vs installed {payload.get('installed_schema_version')}"
            )
        return payload
    except Exception as exc:
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}"}


_IPC_TYPE_CACHE: dict = {"value": None, "reason": None}

IPC_MESSAGE_TYPE = "osu.Game.IPC.ArchiveImportMessage"


def ipc_message_type() -> str:
    """The exact IPC `Type` string the installed lazer compares against.

    osu.Framework's IpcChannel compares the assembly-qualified name byte for byte, and
    the assembly version is part of it, so it is read out of the installed osu.Game.dll
    via the helper. Without the helper (or if it can't read the dll) the name is
    composed from the installed release version, which has always matched so far.
    """
    cached = _IPC_TYPE_CACHE.get("value")
    if cached:
        return cached

    dll = installed_osu_game_dll()
    helper = helper_path()
    if helper and dll:
        try:
            proc = subprocess.run(
                [str(helper), "--ipc-type", str(dll)],
                capture_output=True,
                text=True,
                timeout=180,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            payload = json.loads(proc.stdout.strip() or "{}")
            name = payload.get("ipc_type")
            if name:
                _IPC_TYPE_CACHE["value"] = name
                return name
        except Exception as exc:
            _IPC_TYPE_CACHE["reason"] = f"{type(exc).__name__}: {exc}"

    version = lazer.info().get("version") if hasattr(lazer, "info") else None
    if version:
        parts = str(version).split(".")
        while len(parts) < 4:
            parts.append("0")
        name = f"{IPC_MESSAGE_TYPE}, osu.Game, Version={'.'.join(parts[:4])}, Culture=neutral, PublicKeyToken=null"
        _IPC_TYPE_CACHE["value"] = name
        return name

    raise RuntimeError("could not determine the IPC message type (no helper and no lazer version)")


def backup_realm(data_dir: Path | None = None) -> Path | None:
    data_dir = data_dir or lazer.data_dir()
    if not data_dir:
        return None
    source = Path(data_dir) / "client.realm"
    if not source.is_file():
        return None
    target_dir = config.app_dir() / "realm-backups"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"client-{time.strftime('%Y%m%d-%H%M%S')}.realm"
    shutil.copy2(source, target)
    backups = sorted(target_dir.glob("client-*.realm"), key=lambda p: p.stat().st_mtime, reverse=True)
    for stale in backups[BACKUP_KEEP:]:
        try:
            stale.unlink()
        except OSError:
            pass
    return target


def _run(args: list[str], timeout: float = TIMEOUT) -> tuple[int, dict, str]:
    helper = helper_path()
    if not helper:
        raise RuntimeError("LazerDb helper is not built")
    proc = subprocess.run(
        [str(helper), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        payload = json.loads(proc.stdout.strip() or "{}")
    except json.JSONDecodeError:
        payload = {}
    return proc.returncode, payload, (proc.stderr or "").strip()


def read_collections(data_dir: Path | None = None) -> dict:
    """Current collections in lazer's database (no writes)."""
    data_dir = data_dir or lazer.data_dir()
    if not data_dir:
        raise RuntimeError("lazer data directory not found")
    code, payload, err = _run(["--data-dir", str(data_dir), "--dry-run"])
    if code != 0:
        raise RuntimeError(payload.get("message") or err or f"helper exited {code}")
    return payload


def write_collection(collection_db: Path, data_dir: Path | None = None, *, backup: bool = True) -> dict:
    """Import a collection.db into lazer's database. Returns a report dict."""
    data_dir = data_dir or lazer.data_dir()
    if not data_dir:
        raise RuntimeError("lazer data directory not found")
    status = version_status()
    if not status.get("available"):
        raise RuntimeError(f"cannot write collections: {status.get('reason')}")

    collection_db = Path(collection_db)
    if not collection_db.is_file():
        raise RuntimeError(f"no collection database at {collection_db}")

    backup_path = backup_realm(data_dir) if backup else None
    code, payload, err = _run(["--data-dir", str(data_dir), "--collection", str(collection_db)])
    if code != 0:
        raise RuntimeError(payload.get("message") or err or f"helper exited {code}")

    after = {c["name"]: c["hashes"] for c in payload.get("collections_after") or []}
    before = {c["name"]: c["hashes"] for c in payload.get("collections_before") or []}
    changed = {name: count for name, count in after.items() if before.get(name) != count}
    if not changed:
        # nothing changed: either the collection is already there with these exact
        # hashes (a re-run — success, lazer merges by name) or the write didn't land
        expected = collection_name(collection_db)
        if expected and expected in after:
            payload["unchanged"] = True
            payload["changed"] = {expected: after[expected]}
            payload["backup"] = str(backup_path) if backup_path else None
            return payload
        raise RuntimeError("the helper ran but the collection does not appear in the database afterwards")
    payload["backup"] = str(backup_path) if backup_path else None
    payload["changed"] = changed
    return payload


def import_list_file(paths, directory: Path | None = None) -> Path:
    """Write the helper's `--beatmaps-from` list (one absolute path per line)."""
    directory = Path(directory) if directory is not None else config.app_dir() / "import-lists"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"beatmaps-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}.txt"
    target.write_text("".join(f"{Path(p).resolve()}\n" for p in paths), encoding="utf-8")
    return target


def import_beatmaps(
    paths,
    *,
    data_dir: Path | None = None,
    parallel: int | None = None,
    timeout: float | None = None,
) -> dict:
    """Import `.osz` archives straight into lazer's store + realm, in parallel.

    This runs lazer's own `BeatmapImporter` in the helper process, with
    `Parallel.ForEachAsync` over the whole batch — the same code path the game's import
    screen uses, which is the only one that imports maps in parallel (feeding the
    *running* game one path at a time is serial, ~1 map/s). The importer also deletes
    each archive it imports (`ShouldDeleteArchive` for .osz), which is what the app
    wants when it is set to delete maps after import.

    The game must be closed: both processes write the same realm and file store.

    Returns {requested, imported, failed, seconds, errors}.
    """
    paths = [Path(p) for p in paths]
    if not paths:
        return {"requested": 0, "imported": 0, "failed": 0, "seconds": 0.0, "errors": [], "failed_files": []}

    missing = [str(p) for p in paths if not p.is_file()]
    if missing:
        raise RuntimeError(f"{len(missing)} archive(s) missing, first: {missing[0]}")

    data_dir = data_dir or lazer.data_dir()
    if not data_dir:
        raise RuntimeError("lazer data directory not found")
    status = version_status()
    if not status.get("available"):
        raise RuntimeError(f"cannot import beatmaps directly: {status.get('reason')}")

    list_file = import_list_file(paths)
    args = ["--data-dir", str(data_dir), "--beatmaps-from", str(list_file)]
    if parallel:
        args += ["--parallel", str(max(1, int(parallel)))]
    if timeout is None:
        timeout = max(600.0, 60.0 + 6.0 * len(paths))

    try:
        code, payload, err = _run(args, timeout=timeout)
    finally:
        try:
            list_file.unlink()
        except OSError:
            pass

    if not payload:
        raise RuntimeError(err or f"the import helper exited {code} without a report")

    imported = int(payload.get("beatmaps_imported") or 0)
    failed = int(payload.get("beatmaps_failed") or 0)
    errors = [str(e) for e in (payload.get("errors") or [])]
    if code != 0 and imported == 0 and failed == 0:
        raise RuntimeError(err or f"the import helper exited {code}")
    return {
        "requested": int(payload.get("beatmaps_requested") or len(paths)),
        "imported": imported,
        "failed": failed,
        "seconds": float(payload.get("seconds") or 0.0),
        "errors": errors,
        "failed_files": [str(p) for p in (payload.get("failed_files") or [])],
        "sets_before": payload.get("beatmap_sets_before"),
        "sets_after": payload.get("beatmap_sets_after"),
    }


def collection_name(collection_db: Path) -> str | None:
    """Name of the (first) collection in a legacy collection.db."""
    try:
        from . import collectiondb

        entries = collectiondb.read_collection_db(Path(collection_db))
    except Exception:
        return None
    return entries[0]["name"] if entries else None
