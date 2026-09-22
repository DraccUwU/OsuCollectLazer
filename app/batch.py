"""Prepare a collection folder for lazer's own import screen.

Why this exists: lazer posts exactly ONE progress notification per import *call*
(`RealmArchiveModelImporter.Import(tasks)` → "Beatmap import is initialising..."),
and its IPC file forward imports one file per call — so pushing N maps produces N
tasks. Its "Run setup wizard → Import" screen instead builds a single task array
from a folder of extracted beatmaps, which is the only way to get one task for the
whole batch.

So this module unpacks the collection's `.osz` files into
`<folder>/.lazer-import/Songs/<setId>/` and writes `collection.db` + `osu!.name.cfg`
beside it: one pass of lazer's import screen then imports every map (one task) and
the collection entry (one task).
"""
from __future__ import annotations

import shutil
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import collectiondb

ROOT_NAME = ".lazer-import"
SONGS_DIR = "Songs"


def root_for(folder: str | Path) -> Path:
    return Path(folder) / ROOT_NAME


def _dir_size(path: Path) -> int:
    total = 0
    for f in path.rglob("*"):
        try:
            if f.is_file():
                total += f.stat().st_size
        except OSError:
            pass
    return total


def state(folder: str | Path) -> dict:
    """What is currently staged for the import screen."""
    root = root_for(folder)
    songs = root / SONGS_DIR
    maps = 0
    if songs.is_dir():
        maps = sum(1 for d in songs.iterdir() if d.is_dir() and any(d.glob("*.osu")))
    return {
        "root": str(root),
        "maps": maps,
        "bytes": _dir_size(root) if root.is_dir() else 0,
        "has_db": (root / "collection.db").exists(),
        "ready": maps > 0 and (root / "collection.db").exists(),
    }


def _extract_one(osz: Path, target: Path) -> int:
    """Extract a .osz into `target`. Returns bytes written, or -1 if already there."""
    if target.is_dir() and any(target.glob("*.osu")):
        return -1
    staging = target.parent / (target.name + ".extracting")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=True)
    written = 0
    base = staging.resolve()
    with zipfile.ZipFile(osz) as zf:
        for info in zf.infolist():
            name = info.filename.replace("\\", "/")
            if name.endswith("/"):
                continue
            parts = [p for p in name.split("/") if p not in ("", ".", "..")]
            if not parts:
                continue
            dest = staging.joinpath(*parts)
            # zip-slip guard: never write outside the staging dir
            if not str(dest.resolve()).startswith(str(base)):
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)
            written += info.file_size
    shutil.rmtree(target, ignore_errors=True)
    staging.replace(target)
    return written


def prepare(
    folder: str | Path,
    collection_name: str,
    hashes: list[str],
    *,
    worker: int = 4,
    progress=None,
) -> dict:
    """Stage every `.osz` in `folder` for lazer's import screen.

    Returns {'root', 'maps', 'bytes', 'skipped', 'errors'}.
    """
    folder = Path(folder)
    root = root_for(folder)
    songs = root / SONGS_DIR
    songs.mkdir(parents=True, exist_ok=True)

    files = sorted(folder.glob("*.osz"))
    done = 0
    total = len(files)
    written_total = 0
    skipped = 0
    errors: list[str] = []

    def work(item: Path) -> tuple[str, int, str]:
        target = songs / item.stem
        try:
            n = _extract_one(item, target)
            return item.name, n, ""
        except Exception as exc:  # corrupt archive, disk full, ...
            return item.name, 0, f"{type(exc).__name__}: {exc}"

    with ThreadPoolExecutor(max_workers=max(1, worker)) as pool:
        for name, n, err in pool.map(work, files):
            done += 1
            if err:
                errors.append(f"{name}: {err}")
            elif n < 0:
                skipped += 1
            else:
                written_total += n
            if progress:
                progress(done, total, written_total, err)

    # collection entry + the marker that makes lazer accept this folder
    collectiondb.write_collection_folder(root, collection_name, hashes)

    return {
        "root": str(root),
        "maps": total - len(errors),
        "skipped": skipped,
        "bytes": written_total,
        "errors": errors[:10],
    }


def cleanup(folder: str | Path) -> dict:
    """Delete the staged copies (the .osz library files are untouched)."""
    root = root_for(folder)
    songs = root / SONGS_DIR
    freed = _dir_size(songs) if songs.is_dir() else 0
    shutil.rmtree(songs, ignore_errors=True)
    return {"freed": freed, "root": str(root)}


def sweep_stale(root_dir: str | Path) -> int:
    """Remove half-finished extraction leftovers from previous runs."""
    removed = 0
    root_dir = Path(root_dir)
    if not root_dir.is_dir():
        return 0
    for candidate in root_dir.glob(f"*/{ROOT_NAME}/{SONGS_DIR}/*.extracting"):
        shutil.rmtree(candidate, ignore_errors=True)
        removed += 1
    return removed
