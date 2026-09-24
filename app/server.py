"""Local web server + JSON API for OsuCollectLazer.

Run:  python -m app.server        (or double-click start.bat)
Then: http://127.0.0.1:8765/
"""
from __future__ import annotations

import json
import mimetypes
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import collectiondb, collector, config, lazer, lazerdb, library, setup, shortcuts, version
from .downloader import DownloadJob, Downloader
from .mirrors import MIRRORS, MirrorPool

WEB_DIR = (
    config.resource_dir() / "app" / "web"
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent / "web"
)
SETTINGS = config.load_settings()
LAST_POOL: MirrorPool | None = None  # most recent download pool, for live mirror stats


# ---------------------------------------------------------------------------
# job registry
# ---------------------------------------------------------------------------
def _norm(folder) -> str:
    """Folder identity for job guards (case/separator/relative-path safe)."""
    try:
        return str(Path(folder).resolve()).casefold()
    except Exception:
        return str(folder).casefold()


class Jobs:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, dict] = {}
        self._seq = 0

    def new(self, kind: str, **fields) -> str:
        with self._lock:
            self._seq += 1
            jid = f"{kind}-{self._seq}"
            self._jobs[jid] = {
                "id": jid,
                "kind": kind,
                "status": "running",
                "created_at": time.time(),
                "message": "",
                "log": [],
                **fields,
            }
            return jid

    def update(self, jid: str, **fields) -> None:
        with self._lock:
            if jid in self._jobs:
                self._jobs[jid].update(fields)

    def log(self, jid: str, line: str) -> None:
        with self._lock:
            job = self._jobs.get(jid)
            if job is not None:
                job.setdefault("log", []).append(f"{time.strftime('%H:%M:%S')} {line}")
                del job["log"][:-40]

    def get(self, jid: str) -> dict | None:
        with self._lock:
            return self._jobs.get(jid)

    def has_active_download(self, folder: str) -> bool:
        target = _norm(folder)
        with self._lock:
            for job in self._jobs.values():
                dl = job.get("dl")
                if dl is not None and _norm(dl.folder) == target and dl.status in ("pending", "running"):
                    return True
        return False

    def active_jobs(self, folder: str | None = None) -> list[dict]:
        """Jobs still running, optionally limited to one folder."""
        target = _norm(folder) if folder else None
        with self._lock:
            out = []
            for job in self._jobs.values():
                if job["status"] not in ("pending", "running"):
                    continue
                if target is None or _norm(job.get("folder") or "") == target:
                    out.append({"id": job["id"], "kind": job["kind"], "status": job["status"]})
            return out

    def list(self) -> list[dict]:
        with self._lock:
            jobs = list(self._jobs.values())
        out = []
        for job in sorted(jobs, key=lambda j: j["created_at"], reverse=True)[:20]:
            item = {k: v for k, v in job.items() if k != "dl"}
            dl = job.get("dl")
            if dl is not None:
                stats = dl.progress()
                for key in (
                    "status",
                    "message",
                    "done",
                    "total",
                    "ok",
                    "skipped",
                    "failed",
                    "bytes",
                    "speed",
                    "transfers",
                    "errors",
                    "elapsed",
                    "folder",
                    "name",
                ):
                    if key in stats:
                        item[key] = stats[key]
                item["id"] = job["id"]
                item["timeline"] = dl.timeline[-40:]
            out.append(item)
        return out


JOBS = Jobs()
CANCELS: dict[str, threading.Event] = {}


# ---------------------------------------------------------------------------
# job runners
# ---------------------------------------------------------------------------
def start_download(ref: str) -> dict:
    cid = collector.parse_ref(ref)
    if not cid:
        raise ValueError("could not read a collection id from that input")
    collection = collector.fetch_collection(cid)
    summary = collector.summarize(collection)
    if not summary["set_ids"]:
        raise ValueError("that collection has no beatmapsets to download")

    safe_name = "".join("_" if ch in '/\\:*?"<>|' else ch for ch in (summary["name"] or "collection")).strip() or "collection"
    folder = config.download_dir(SETTINGS) / f"{safe_name}-{cid}"
    folder.mkdir(parents=True, exist_ok=True)
    if JOBS.has_active_download(str(folder)):
        raise ValueError("that collection is already downloading")
    library.save_meta(
        folder,
        {
            "collection_id": cid,
            "name": summary["name"],
            "url": f"{collector.BASE}/collections/{cid}",
            "uploader": summary["uploader"],
            "set_count": summary["set_count"],
            "checksum_count": summary["checksum_count"],
            "beatmap_count": summary["beatmap_count"],
            "downloaded_at": time.time(),
        },
    )

    job = DownloadJob(
        collection,
        summary,
        folder,
        no_video=bool(SETTINGS.get("no_video", True)),
        concurrency=SETTINGS.get("concurrency", 8),
        verify=bool(SETTINGS.get("verify_zips", True)),
        mirrors=SETTINGS.get("mirrors"),
        name_prefix=SETTINGS.get("collection_prefix", ""),
    )
    jid = JOBS.new(
        "download",
        name=job.entry_name,
        folder=str(folder),
        dl=job,
        total=len(summary["set_ids"]),
        done=0,
    )
    cancel = threading.Event()
    CANCELS[jid] = cancel

    def run() -> None:
        global LAST_POOL
        try:
            pool = MirrorPool(SETTINGS.get("mirrors"))
            LAST_POOL = pool
            try:
                probe = pool.probe(no_video=bool(SETTINGS.get("no_video", True)))
                order = ", ".join(
                    f"{n} {probe[n]:.0f}ms" if n in probe and probe[n] != float("inf") else f"{n} n/a"
                    for n in pool.names
                )
                JOBS.log(jid, f"mirror probe → fastest first: {order}")
            except Exception as exc:
                JOBS.log(jid, f"mirror probe skipped: {exc}")
            # One-click pipeline: with streaming on, each wave of maps is imported (and
            # deleted) while the download is still running instead of after it.
            pipeline_mode = SETTINGS.get("pipeline_mode", "auto")
            streaming = pipeline_mode == "auto" and bool(SETTINGS.get("stream_import", True))
            if streaming:
                try:
                    start_finalize(str(folder), wait_for=lambda: _download_active(jid))
                    JOBS.log(jid, "streaming import started — maps are imported as they land")
                except Exception as exc:
                    JOBS.log(jid, f"streaming import skipped: {exc}")

            Downloader(pool, cancel).run(job)
            # write the collection entry + config marker once maps are on disk
            if job.counts()["ok"] + job.counts()["skipped"] > 0:
                meta = collectiondb.write_collection_folder(
                    folder,
                    job.entry_name,
                    job.summary["checksums"],
                )
                library.save_meta(
                    folder,
                    {
                        **library.load_meta(folder),
                        "collection_name": job.entry_name,
                        "collection_db_collections": meta["collections"],
                    },
                )
            JOBS.update(jid, status=job.status, message=job.message)
            JOBS.log(jid, job.message)
            mode = SETTINGS.get("pipeline_mode", "auto")
            if mode == "auto":
                if _import_active(folder):
                    JOBS.log(jid, "import job still working through the maps — it writes the collection too")
                else:
                    JOBS.log(jid, "one-click pipeline: importing maps, then writing the collection into lazer")
                    try:
                        start_finalize(str(folder))
                    except Exception as exc:
                        JOBS.log(jid, f"finalize skipped: {exc}")
            else:
                JOBS.log(jid, "pipeline mode: manual — press 'import now' in the Library when you want the maps in")
        except Exception as exc:
            JOBS.update(jid, status="failed", message=f"{type(exc).__name__}: {exc}")
            JOBS.log(jid, f"failed: {exc}")
        finally:
            CANCELS.pop(jid, None)

    threading.Thread(target=run, daemon=True, name=f"download-{jid}").start()
    return JOBS.get(jid) or {}


_LAZERDB_STATUS_CACHE: dict = {"at": 0.0, "payload": None}


def _collections_payload() -> dict:
    payload = lazerdb.read_collections()
    return {
        "collections": payload.get("collections_before") or payload.get("collections_after") or [],
        "schema_version": payload.get("schema_version"),
        "realm": payload.get("realm"),
    }


def lazerdb_status(max_age: float = 300.0) -> dict:
    """Cached "is the import helper usable?" answer (it spawns a helper process)."""
    payload = _LAZERDB_STATUS_CACHE.get("payload")
    if payload is None or (time.time() - float(_LAZERDB_STATUS_CACHE.get("at") or 0)) > max_age:
        try:
            payload = lazerdb.version_status()
        except Exception as exc:
            payload = {"available": False, "reason": f"{type(exc).__name__}: {exc}"}
        _LAZERDB_STATUS_CACHE["payload"] = payload
        _LAZERDB_STATUS_CACHE["at"] = time.time()
    return payload


def _fmt_bytes(n: float) -> str:
    n = float(n or 0)
    for unit, scale in (("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if n >= scale:
            return f"{n / scale:.1f} {unit}"
    return f"{n:.0f} B"


def _download_active(jid: str) -> bool:
    """True while the download job for this collection is still running."""
    job = JOBS.get(jid) or {}
    return job.get("status") in ("pending", "running")


def _import_active(folder: Path) -> bool:
    return any(job["kind"] == "import" for job in JOBS.active_jobs(str(folder)))


def _ensure_lazer_closed(jid: str | None = None) -> None:
    """Every write into lazer's data needs the game closed; close it unless that is off.

    `jid` is the job the close is narrated in — the manual "write collection" endpoint
    has no job and passes nothing.
    """
    if not lazer.is_running():
        return
    if not SETTINGS.get("close_lazer_before_import", True):
        raise RuntimeError(
            "osu!lazer is running and closing it automatically is turned off — close the game "
            "(or re-enable that setting) and try again"
        )

    def log(line: str) -> None:
        if jid:
            JOBS.log(jid, line)

    if jid:
        JOBS.update(jid, stage="closing osu!lazer")
    log("osu!lazer is running — closing it, since the app writes straight into its files")
    ok, how = lazer.close_lazer()
    if not ok:
        raise RuntimeError(f"could not close osu!lazer: {how}")
    if how == "force-closed":
        log(
            "osu!lazer ignored a normal close request (it was busy), so it was terminated — "
            "anything unsaved in the game is gone"
        )
    else:
        log("osu!lazer closed")


def start_finalize(folder: str, *, skip_import: bool = False, wait_for=None) -> dict:
    """The one-click pipeline: import the maps into lazer's files + database with the
    helper (lazer's own importer, in parallel), delete the archives as they are taken,
    then write the collection entry. osu!lazer never has to be open.

    `wait_for` (optional callable) keeps the job alive while it returns True, so each wave
    is imported *while the download is still running* instead of after it.
    """
    path = Path(folder)
    files = sorted(path.glob("*.osz"))
    meta = library.load_meta(path)
    db_path = path / "collection.db"
    jid = JOBS.new(
        "import",
        name=path.name,
        folder=str(path),
        total=int(meta.get("set_count") or len(files)),
        done=0,
        imported=0,
        deleted=0,
        freed=0,
        stage="starting",
    )
    cancel = threading.Event()
    CANCELS[jid] = cancel

    def run() -> None:
        imported = deleted = freed = 0
        failed_imports = 0
        handled: set[str] = set()
        notes: list[str] = []
        try:
            chunk = max(1, int(SETTINGS.get("import_chunk", 250)))
            expected = int(meta.get("set_count") or len(files))
            skipped_import = bool(skip_import)
            announced = False

            while True:
                if cancel.is_set():
                    raise RuntimeError("cancelled")

                pending = [f for f in sorted(path.glob("*.osz")) if f.name not in handled]

                if pending and not skipped_import:
                    window, rest = pending[:chunk], pending[chunk:]
                    if not announced:
                        announced = True
                        JOBS.log(
                            jid,
                            "importing with lazer's own importer, in parallel, straight into its "
                            "files + database — the game does not need to be running",
                        )

                    _ensure_lazer_closed(jid)

                    sizes = {str(p): (p.stat().st_size if p.exists() else 0) for p in window}
                    JOBS.update(jid, stage=f"importing {len(window)} maps")

                    report = lazerdb.import_beatmaps(window)
                    confirmed = int(report["imported"])
                    failed = int(report["failed"])
                    seconds = float(report["seconds"] or 0.0)
                    # the helper reports resolved absolute paths while `window` can hold
                    # relative ones — compare normalised identities, or a failed archive
                    # whose path shapes differ would be deleted despite the failure
                    failed_paths = {_norm(p) for p in (report.get("failed_files") or [])}
                    failed_names = {Path(p).name for p in (report.get("failed_files") or [])}

                    rate = confirmed / seconds if seconds else 0.0
                    sets = ""
                    if report.get("sets_before") is not None and report.get("sets_after") is not None:
                        sets = f" [lazer beatmapsets {report['sets_before']} → {report['sets_after']}]"
                    JOBS.log(
                        jid,
                        f"{confirmed} imported, {failed} failed in {seconds:.1f}s"
                        + (f" ({rate:.1f} maps/s)" if seconds else "")
                        + sets,
                    )
                    for err in report["errors"][:3]:
                        notes.append(str(err))
                        JOBS.log(jid, str(err))

                    imported += confirmed
                    failed_imports += failed

                    # the importer deletes the archives it takes (lazer's
                    # ShouldDeleteArchive); this accounts for the space and removes the rest
                    freed_here = 0
                    kept = 0
                    for f in window:
                        if _norm(f) in failed_paths or f.name in failed_names:
                            notes.append(f"kept {f.name} (import failed — the file stays for a retry)")
                            JOBS.log(jid, notes[-1])
                            kept += 1
                            continue
                        size = sizes.get(str(f), 0)
                        if f.exists():
                            try:
                                f.unlink()
                            except OSError as exc:
                                notes.append(f"could not delete {f.name}: {exc}")
                                continue
                        deleted += 1
                        freed_here += size
                    freed += freed_here
                    JOBS.log(
                        jid,
                        f"{confirmed} imported — {len(window) - kept} archives gone "
                        f"({_fmt_bytes(freed_here)} freed)"
                        + (f", {kept} kept after failures" if kept else ""),
                    )

                    handled.update(f.name for f in window)
                    JOBS.update(
                        jid,
                        imported=imported,
                        done=len(handled),
                        deleted=deleted,
                        freed=freed,
                        total=max(expected, len(handled) + len(rest)),
                    )
                    continue

                if wait_for is not None and wait_for():
                    JOBS.update(
                        jid,
                        stage=f"waiting for the download — {len(handled)} maps imported so far",
                        total=max(expected, len(handled)),
                    )
                    time.sleep(3)
                    continue

                break

            collection_result = None
            collection_mode = SETTINGS.get("collection_mode", "database")
            if db_path.exists() and collection_mode == "database":
                JOBS.update(jid, stage="writing the collection into lazer's database")
                try:
                    # the collection entry goes into the realm as well, and two writers on
                    # one realm is a corrupt realm — close the game here too, in case it
                    # was opened again while the download ran
                    _ensure_lazer_closed(jid)
                    collection_result = lazerdb.write_collection(db_path)
                    changed = collection_result.get("changed") or {}
                    if collection_result.get("unchanged"):
                        JOBS.log(jid, f"collection already in lazer's database: {changed}")
                    else:
                        JOBS.log(jid, f"collection written into lazer's database: {changed}")
                except Exception as exc:
                    notes.append(f"collection write failed: {exc}")
                    JOBS.log(jid, notes[-1])
            elif db_path.exists():
                notes.append("collection left in the folder for lazer's import screen (collection_mode=off)")

            library.record_import(
                path,
                imported=imported,
                deleted=deleted,
                freed=freed,
                failed=failed_imports,
                collection=collection_result.get("changed") if collection_result else None,
                notes=notes,
            )
            message = f"{imported} maps imported"
            if deleted:
                message += f", {deleted} .osz deleted ({_fmt_bytes(freed)} freed)"
            if collection_result:
                message += (
                    ", collection already in lazer's database"
                    if collection_result.get("unchanged")
                    else ", collection written into lazer's database"
                )
            if failed_imports:
                message += f", {failed_imports} import failures"
            JOBS.update(jid, status="done" if not notes else "partial", message=message, stage="done")
            JOBS.log(jid, message)
        except Exception as exc:
            JOBS.update(jid, status="failed", message=f"{type(exc).__name__}: {exc}", stage="failed")
            JOBS.log(jid, f"failed: {exc}")
        finally:
            CANCELS.pop(jid, None)

    threading.Thread(target=run, daemon=True, name=f"import-{jid}").start()
    return JOBS.get(jid) or {}


def _download_root() -> Path:
    return config.download_dir(SETTINGS).resolve()


def _is_collection_folder(path: Path) -> bool:
    """True when `path` is a collection folder: strictly inside the download root.

    The root itself does not qualify — every endpoint that takes a folder is aimed at
    one collection, and `scope: all` is the only way to touch the whole root.
    """
    try:
        resolved = path.resolve()
    except OSError:
        return False
    root = _download_root()
    if _norm(resolved) == _norm(root):
        return False
    try:
        resolved.relative_to(root)
        return True
    except ValueError:
        return False


def _path_size(path: Path) -> int:
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    for f in path.rglob("*"):
        try:
            if f.is_file():
                total += f.stat().st_size
        except OSError:
            pass
    return total


def delete_library(scope: str = "all", folder: str | None = None, dry_run: bool = False) -> dict:
    """Delete downloaded data.

    Deletes whole collection folders (maps, collection.db, bookkeeping); `folder` limits
    the operation to one collection folder — never the root — and `dry_run` only reports
    sizes. Never touches lazer's own data or the app's settings.
    """
    root = _download_root()
    if folder:
        target = Path(folder)
        if not _is_collection_folder(target):
            raise PermissionError(
                "folder must be a collection folder inside the download directory "
                "(use scope 'all' to clear the whole library)"
            )
        if target.exists() and not target.is_dir():
            raise ValueError(f"{target.name} is not a collection folder")
        folders = [target] if target.is_dir() else []  # an already-deleted folder is a no-op
    else:
        folders = sorted(p for p in root.iterdir() if p.is_dir())

    if not dry_run:
        if folder:
            busy = JOBS.active_jobs(str(Path(folder)))
            if busy:
                raise RuntimeError(
                    f"{busy[0]['kind']} is still running for that collection — wait for it first"
                )
        else:
            active = [j for j in JOBS.list() if j["status"] in ("running", "pending")]
            if active:
                raise RuntimeError(
                    f"{len(active)} job(s) still running — wait for or cancel them before deleting"
                )

    freed = 0
    removed = 0
    leftover: list[str] = []

    for candidate in folders:
        freed += _path_size(candidate)
        if dry_run:
            removed += 1
            continue
        shutil.rmtree(candidate, ignore_errors=True)
        if candidate.exists():
            leftover.append(candidate.name)
        else:
            removed += 1

    # stray files sitting directly in the download root belong to a whole-library
    # delete; a single-collection delete must not sweep them
    strays = [] if folder else [f for f in root.iterdir() if f.is_file()]
    for f in strays:
        size = _path_size(f)
        if dry_run:
            freed += size
            removed += 1
            continue
        try:
            f.unlink()
            freed += size
            removed += 1
        except OSError:
            leftover.append(f.name)

    return {
        "scope": scope,
        "dry_run": dry_run,
        "removed": removed,
        "freed": freed,
        "leftover": leftover[:10],
        "download_dir": str(root),
    }


def api_get(path: str, query: dict) -> tuple[int, dict]:
    if path == "/api/setup":
        return 200, setup.state()
    if path == "/api/shortcuts":
        return 200, shortcuts.status()
    if path == "/api/status":
        return 200, {
            "version": version.__version__,
            "setup_complete": bool(SETTINGS.get("setup_complete")),
            "lazer": lazer.info(SETTINGS),
            "settings": SETTINGS,
            "lazerdb": lazerdb_status(),
            "download_dir": str(_download_root()),
            "mirrors": [
                {"name": name, "enabled": name in (SETTINGS.get("mirrors") or []), "cooldown": cfg["cooldown"]}
                for name, cfg in MIRRORS.items()
            ],
            "library_count": len(library.scan(_download_root())),
        }
    if path == "/api/search":
        q = (query.get("q") or [""])[0]
        page = int((query.get("page") or ["1"])[0])
        sort = (query.get("sort") or [None])[0]
        return 200, collector.search(q, page=page, sort_by=sort)
    if path == "/api/collection":
        ref = (query.get("ref") or query.get("id") or [""])[0]
        cid = collector.parse_ref(ref)
        if not cid:
            raise ValueError("no collection id in request")
        collection = collector.fetch_collection(cid)
        summary = collector.summarize(collection)
        summary["checksums"] = summary["checksums"][:0]  # keep the payload small
        summary["set_ids"] = summary["set_ids"][:0]
        return 200, summary
    if path == "/api/jobs":
        return 200, {"jobs": JOBS.list()}
    if path == "/api/library":
        return 200, {
            "collections": library.scan(_download_root()),
            "download_dir": str(_download_root()),
            "pipeline": {
                "pipeline_mode": SETTINGS.get("pipeline_mode", "auto"),
                "collection_mode": SETTINGS.get("collection_mode", "database"),
                "stream_import": bool(SETTINGS.get("stream_import", True)),
                "close_lazer_before_import": bool(SETTINGS.get("close_lazer_before_import", True)),
                "lazerdb": lazerdb_status(),
            },
        }
    if path == "/api/collection/lazer":
        return 200, _collections_payload()
    if path == "/api/mirrors":
        pool = LAST_POOL
        return 200, {
            "live": pool.snapshot() if pool else [],
            "cooling": pool.cooling() if pool else {},
            "configured": [
                {"name": name, "enabled": name in (SETTINGS.get("mirrors") or []), "cooldown": cfg["cooldown"]}
                for name, cfg in MIRRORS.items()
            ],
        }
    raise KeyError(path)


def api_post(path: str, body: dict) -> tuple[int, dict]:
    if path == "/api/download":
        job = start_download(str(body.get("ref") or body.get("id") or ""))
        return 200, {"job": JOBS.list()[0], "started": True}
    if path == "/api/library/delete":
        # no default: a request that does not name its scope must never delete anything
        scope = str(body.get("scope") or "")
        if scope != "all":
            return 400, {"error": 'scope must be "all" — an explicit scope is required'}
        try:
            result = delete_library(
                scope=scope,
                folder=(str(body["folder"]) if body.get("folder") else None),
                dry_run=bool(body.get("dry_run")),
            )
        except ValueError as exc:
            return 400, {"error": str(exc)}
        except PermissionError as exc:
            return 403, {"error": str(exc)}
        except RuntimeError as exc:
            return 409, {"error": str(exc)}
        return 200, result
    if path == "/api/mirrors/probe":
        pool = MirrorPool(SETTINGS.get("mirrors"))
        probe = pool.probe(no_video=bool(SETTINGS.get("no_video", True)))
        global LAST_POOL
        LAST_POOL = pool
        return 200, {
            "order": [
                {"name": n, "ms": (None if probe.get(n, float("inf")) == float("inf") else round(probe[n]))}
                for n in pool.names
            ]
        }
    if path == "/api/jobs/cancel":
        jid = str(body.get("id") or "")
        cancel = CANCELS.get(jid)
        if cancel:
            cancel.set()
            return 200, {"cancelled": True}
        return 404, {"error": "unknown or finished job"}
    if path == "/api/setup/pick":
        kind = str(body.get("kind") or "folder")
        if kind not in ("folder", "file"):
            return 400, {"error": "kind must be folder or file"}
        return 200, {"path": setup.pick_path(kind, str(body.get("start") or "") or None)}
    if path == "/api/setup/helper":
        try:
            return 200, setup.install_helper(str(body.get("method") or "auto"))
        except ValueError as exc:
            return 400, {"error": str(exc)}
        except RuntimeError as exc:
            return 409, {"error": str(exc)}
        except Exception as exc:
            return 500, {"error": f"{type(exc).__name__}: {exc}"}
    if path == "/api/setup/finish":
        saved = setup.apply_settings(body.get("settings") or {})
        SETTINGS.clear()
        SETTINGS.update(saved)
        return 200, {"settings": SETTINGS, "setup_complete": True}
    if path == "/api/settings":
        updates = body.get("settings") or {}
        for key in config.DEFAULTS:
            if key in updates:
                SETTINGS[key] = updates[key]
        config.save_settings(SETTINGS)
        return 200, {"settings": SETTINGS}
    if path == "/api/finalize":
        folder = Path(str(body.get("folder") or ""))
        if not _is_collection_folder(folder):
            return 403, {"error": "folder must be a collection folder inside the download directory"}
        busy = JOBS.active_jobs(str(folder))
        if busy:
            return 409, {"error": f"{busy[0]['kind']} already running for that collection"}
        start_finalize(str(folder), skip_import=bool(body.get("skip_import")))
        return 200, {"started": True}
    if path == "/api/collection/lazer":
        return 200, _collections_payload()
    if path == "/api/collection/write":
        folder = Path(str(body.get("folder") or ""))
        if not _is_collection_folder(folder):
            return 403, {"error": "folder must be a collection folder inside the download directory"}
        db_path = folder / "collection.db"
        if not db_path.exists():
            return 400, {"error": "no collection.db in that folder yet"}
        try:
            _ensure_lazer_closed()
        except RuntimeError as exc:
            return 409, {"error": str(exc)}
        try:
            result = lazerdb.write_collection(db_path)
        except Exception as exc:
            return 400, {"error": f"{type(exc).__name__}: {exc}"}
        library.record_import(folder, collection=result.get("changed"), imported=0, deleted=0, freed=0, failed=0, notes=[])
        return 200, {"changed": result.get("changed"), "backup": result.get("backup")}
    if path == "/api/shortcuts":
        where = str(body.get("where") or "")
        action = str(body.get("action") or "create")
        if action not in ("create", "remove"):
            return 400, {"error": "action must be create or remove"}
        try:
            result = shortcuts.create(where) if action == "create" else shortcuts.remove(where)
        except ValueError as exc:
            return 400, {"error": str(exc)}
        except RuntimeError as exc:
            return 409, {"error": str(exc)}
        except Exception as exc:
            return 500, {"error": f"{type(exc).__name__}: {exc}"}
        return 200, {**result, "status": shortcuts.status()}
    if path == "/api/open":
        link = str(body.get("url") or "")
        if link:
            # the app window cannot show a website: send links to the real browser
            if not link.lower().startswith(("http://", "https://")):
                return 400, {"error": "only http(s) links can be opened"}
            webbrowser.open(link)
            return 200, {"opened": link}
        folder = Path(str(body.get("folder") or ""))
        if not _is_collection_folder(folder):
            return 403, {"error": "folder must be a collection folder inside the download directory"}
        if os.name == "nt":
            os.startfile(str(folder))  # noqa: S606 - local convenience
        else:
            subprocess.Popen(["xdg-open", str(folder)])
        return 200, {"opened": str(folder)}
    raise KeyError(path)


# ---------------------------------------------------------------------------
# HTTP plumbing
# ---------------------------------------------------------------------------
_LOOPBACK_NAMES = {"127.0.0.1", "localhost", "[::1]"}


def _host_allowed(host: str | None) -> bool:
    """True when the request's Host names a loopback address (or is absent).

    Checked for reads as well as writes: a page that DNS-rebinds to 127.0.0.1 carries its
    own name in Host, and this app is only ever reached as `127.0.0.1` or `localhost`.
    """
    if not host:
        return True
    return host.rsplit(":", 1)[0].lower() in _LOOPBACK_NAMES


def _origin_allowed(origin: str | None, host: str | None, port: int) -> bool:
    """Same-origin check for state-changing requests to a loopback server.

    Only the loopback names count. A page that DNS-rebinds to 127.0.0.1 is same-origin in
    the browser and carries its *own* name in both headers, so nothing about the request
    may be taken on trust: a present Host has to be a loopback name, and a present Origin
    has to be one of the loopback origins for this port. Requests without either header
    (curl, other local tools) still pass.
    """
    if not _host_allowed(host):
        return False
    if not origin:
        return True
    allowed = {f"http://{name}:{port}" for name in _LOOPBACK_NAMES}
    return origin.rstrip("/").lower() in allowed


def _static_file(rel: str) -> Path | None:
    """The UI file for a request path, or None when it escapes the web directory.

    Resolved and containment-checked, so `..` in any spelling (either separator) cannot
    walk out of `app/web`: the static route serves the UI and nothing else.
    """
    try:
        candidate = (WEB_DIR / rel).resolve()
    except (OSError, ValueError):
        return None
    if not candidate.is_relative_to(WEB_DIR.resolve()):
        return None
    return candidate if candidate.is_file() else None


class Handler(BaseHTTPRequestHandler):
    server_version = "OsuCollectLazer"

    def log_message(self, fmt, *args):  # keep the console readable
        pass

    def _send(self, code: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_file(self, file_path: Path) -> None:
        if not file_path.is_file():
            self.send_error(404)
            return
        data = file_path.read_bytes()
        ctype = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        if file_path.suffix in (".html", ".js", ".css"):
            ctype += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        """The request's JSON body — or a ValueError.

        Strict on purpose: a missing or malformed body must never be read as a valid
        (and, on the delete endpoint, destructive) request. Requiring this Content-Type
        doubles as the CSRF guard: a foreign page cannot send application/json without
        a preflight, and nothing here ever approves one.
        """
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            raise ValueError("a JSON body is required")
        if ctype != "application/json":
            raise ValueError("Content-Type must be application/json for the local API")
        try:
            parsed = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid JSON body: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError("the request body must be a JSON object")
        return parsed

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if not _host_allowed(self.headers.get("Host")):
            self._send(403, {"error": "requests must come from 127.0.0.1 or localhost"})
            return
        query = parse_qs(parsed.query)
        if parsed.path.startswith("/api/"):
            try:
                code, payload = api_get(parsed.path, query)
            except KeyError:
                code, payload = 404, {"error": f"unknown endpoint {parsed.path}"}
            except collector.CollectorError as exc:
                code, payload = 404, {"error": str(exc)}
            except Exception as exc:
                code, payload = 500, {"error": f"{type(exc).__name__}: {exc}", "trace": traceback.format_exc()[-800:]}
            self._send(code, payload)
            return
        target = _static_file(parsed.path.lstrip("/") or "index.html")
        if target is None:
            self.send_error(404)
            return
        self._send_file(target)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if not _origin_allowed(self.headers.get("Origin"), self.headers.get("Host"), self.server.server_port):
            self._send(403, {"error": "cross-origin request refused"})
            return
        try:
            body = self._body()
        except ValueError as exc:
            self._send(400, {"error": str(exc)})
            return
        try:
            code, payload = api_post(parsed.path, body)
        except KeyError:
            code, payload = 404, {"error": f"unknown endpoint {parsed.path}"}
        except collector.CollectorError as exc:
            code, payload = 404, {"error": str(exc)}
        except ValueError as exc:
            code, payload = 400, {"error": str(exc)}
        except Exception as exc:
            code, payload = 500, {"error": f"{type(exc).__name__}: {exc}", "trace": traceback.format_exc()[-800:]}
        self._send(code, payload)


class Server(ThreadingHTTPServer):
    # Deliberately NOT reusing the address: with SO_REUSEADDR on Windows several
    # instances can bind the same port and then race each other's job state
    # (and each other's .part files). Better to refuse to start a second one.
    allow_reuse_address = False


def bind(port: int) -> Server | None:
    """Take the port, or None when something else already holds it."""
    try:
        return Server(("127.0.0.1", port), Handler)
    except OSError:
        return None


def main(argv: list[str] | None = None) -> int:
    port = int(SETTINGS.get("port", 8765))
    argv = argv if argv is not None else []
    if "--version" in argv:
        print(f"OsuCollectLazer {version.__version__}")
        return 0
    if "--port" in argv:
        port = int(argv[argv.index("--port") + 1])
    server = bind(port)
    if server is None:
        url = f"http://127.0.0.1:{port}/"
        print(f"Could not bind {url} (port in use).")
        print("Another OsuCollectLazer is probably already running — just open the URL above.")
        return 1
    url = f"http://127.0.0.1:{port}/"
    status = lazerdb_status()
    helper = "ready" if status.get("available") else f"NOT READY ({status.get('reason', 'unknown')})"
    print(f"OsuCollectLazer {version.__version__} running at {url}")
    print(f"  download dir : {_download_root()}")
    print(f"  lazer        : {lazer.info(SETTINGS)}")
    print(f"  map import   : direct + parallel via the helper — {helper}")
    print("Stop with Ctrl+C")
    if "--no-browser" not in argv:
        threading.Thread(target=lambda: (time.sleep(0.6), webbrowser.open(url)), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
