"""Downloaded-collection library: scanning the download dir + per-folder metadata."""
from __future__ import annotations

import json
import time
from pathlib import Path

from . import batch


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, payload) -> None:
    try:
        path.write_text(json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def save_meta(folder: Path, meta: dict) -> None:
    _write_json(folder / "meta.json", meta)


def load_meta(folder: Path) -> dict:
    return _read_json(folder / "meta.json", {})


def load_import_state(folder: Path) -> dict:
    return _read_json(folder / "import-state.json", {"pushed": {}, "collection_imported": False})


def save_import_state(folder: Path, state: dict) -> None:
    state["updated_at"] = time.time()
    _write_json(folder / "import-state.json", state)


def record_push(folder: Path, pushed: list[str], failed: list[str], errors: list[str] | None = None) -> dict:
    state = load_import_state(folder)
    pushed_map = state.get("pushed") or {}
    for name in pushed:
        pushed_map[name] = "ok"
    for name in failed:
        pushed_map.setdefault(name, "failed")
    state["pushed"] = pushed_map
    state["last_push"] = {"at": time.time(), "failed": len(failed), "errors": (errors or [])[:5]}
    save_import_state(folder, state)
    return state


def record_import(
    folder: Path,
    *,
    imported: int = 0,
    deleted: int = 0,
    freed: int = 0,
    failed: int = 0,
    collection: dict | None = None,
    notes: list[str] | None = None,
) -> dict:
    """Remember what the one-click import did (shown in the Library)."""
    folder = Path(folder)
    state = load_import_state(folder)
    state["last_import"] = {
        "at": time.time(),
        "imported": imported,
        "deleted": deleted,
        "freed": freed,
        "failed": failed,
        "notes": (notes or [])[:5],
    }
    if imported or deleted:
        state["maps_imported"] = state.get("maps_imported", 0) + imported
        state["maps_deleted"] = state.get("maps_deleted", 0) + deleted
        state["maps_freed"] = state.get("maps_freed", 0) + freed
    if collection:
        state["collection_in_lazer"] = collection
        state["collection_written_at"] = time.time()
        state["collection_imported"] = True
    save_import_state(folder, state)
    return state


def scan(download_dir: Path) -> list[dict]:
    """List collection folders with their status, newest first."""
    out: list[dict] = []
    if not download_dir.is_dir():
        return out
    for folder in sorted(download_dir.iterdir(), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True):
        if not folder.is_dir():
            continue
        osz = sorted(folder.glob("*.osz"))
        if not osz and not (folder / "collection.db").exists():
            continue
        meta = load_meta(folder)
        state = load_import_state(folder)
        pushed_map = state.get("pushed") or {}
        pushed = sum(1 for v in pushed_map.values() if v == "ok")
        failed = sum(1 for v in pushed_map.values() if v == "failed")
        staged = batch.state(folder)
        size = 0
        for f in osz:
            try:
                size += f.stat().st_size
            except OSError:
                pass
        out.append(
            {
                "folder": str(folder),
                "name": meta.get("name") or folder.name,
                "collection_id": meta.get("collection_id"),
                "url": meta.get("url"),
                "downloaded_at": meta.get("downloaded_at"),
                "beatmapsets": len(osz),
                "expected_sets": meta.get("set_count"),
                "checksums": meta.get("checksum_count"),
                "size_bytes": size,
                "has_collection_db": (folder / "collection.db").exists(),
                "importable_by_lazer": _importable(folder),
                "maps_pushed": pushed,
                "maps_push_failed": failed,
                "collection_imported": bool(state.get("collection_imported")),
                "collection_in_lazer": state.get("collection_in_lazer"),
                "last_import": state.get("last_import"),
                "maps_imported": state.get("maps_imported", 0),
                "maps_deleted": state.get("maps_deleted", 0),
                "maps_freed": state.get("maps_freed", 0),
                "batch": staged,
                # what to point lazer's import screen at: staged folder takes maps
                # (one task) + the collection in one pass; the plain folder is
                # collection-only.
                "import_folder": staged["root"] if staged["ready"] else str(folder),
                "modified": folder.stat().st_mtime,
            }
        )
    return out


def _importable(folder: Path) -> bool:
    if any(folder.glob("osu!.*.cfg")):
        return True
    return (folder / "Songs").is_dir() or (folder / "Skins").is_dir()
