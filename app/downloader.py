"""Parallel, multi-mirror beatmapset downloader with resume and integrity checks.

One `.osz` per beatmapset, named `<setId>.osz`, written into the collection folder.
A `.part` file marks an in-flight download; a complete file is verified as a zip
containing at least one `.osu` before it is accepted.
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .mirrors import MirrorPool

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
MAX_PASSES_PER_SET = 6
STALL_TIMEOUT = 20  # seconds without a byte before the mirror is dropped
SLOW_GRACE = 15.0  # seconds a transfer may run before the speed floor applies
MIN_SPEED = 300_000  # bytes/s floor after the grace period (last pass is exempt)


class SlowTransfer(Exception):
    """A mirror that is technically alive but uselessly slow."""


class DownloadJob:
    """State of one collection download; safe to read from other threads."""

    def __init__(
        self,
        collection: dict,
        summary: dict,
        folder: Path,
        *,
        no_video: bool = True,
        concurrency: int = 8,
        verify: bool = True,
        mirrors: list[str] | None = None,
        name_prefix: str = "",
        kind: str = "download",
    ):
        self.kind = kind
        self.collection = collection
        self.summary = summary
        self.folder = folder
        self.no_video = no_video
        self.concurrency = max(1, int(concurrency))
        self.verify = verify
        self.mirror_names = mirrors or []
        self.name_prefix = name_prefix
        self.entry_name = f"{name_prefix}{summary.get('name') or collection.get('name') or 'collection'}"
        self.status = "pending"  # pending|running|done|failed|cancelled
        self.message = ""
        self.sets: dict[int, str] = {sid: "pending" for sid in summary["set_ids"]}
        self.errors: dict[int, str] = {}
        self.bytes_done = 0
        self.timeline: list[dict] = []  # finished transfers: {sid, mirror, seconds, bytes, speed}
        self._byte_samples: list[tuple[float, int]] = [(time.time(), 0)]
        self.created_at = time.time()
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.lock = threading.Lock()

    # -- state helpers ------------------------------------------------------
    def set_state(self, sid: int, state: str, error: str | None = None) -> None:
        with self.lock:
            self.sets[sid] = state
            if error:
                self.errors[sid] = error

    def add_bytes(self, n: int) -> None:
        with self.lock:
            self.bytes_done += n
            self._byte_samples.append((time.time(), self.bytes_done))
            if len(self._byte_samples) > 400:
                del self._byte_samples[:200]

    def speed_now(self, window: float = 20.0) -> float:
        """Bytes/s over the last `window` seconds (0 until there is data)."""
        with self.lock:
            samples = list(self._byte_samples)
        if len(samples) < 2:
            return 0.0
        now_t, now_b = samples[-1]
        for t, b in reversed(samples):
            if now_t - t >= window:
                dt = now_t - t
                return (now_b - b) / dt if dt > 0 else 0.0
        t0, b0 = samples[0]
        dt = now_t - t0
        return (now_b - b0) / dt if dt > 0 else 0.0

    def record_transfer(self, sid: int, mirror: str, seconds: float, nbytes: int) -> None:
        with self.lock:
            self.timeline.append(
                {
                    "sid": sid,
                    "mirror": mirror,
                    "seconds": round(seconds, 2),
                    "bytes": nbytes,
                    "speed": round(nbytes / seconds) if seconds > 0 else 0,
                }
            )
            del self.timeline[:-400]

    def counts(self) -> dict:
        with self.lock:
            states = list(self.sets.values())
            return {
                "total": len(states),
                "ok": states.count("ok"),
                "skipped": states.count("skipped"),
                "failed": states.count("failed"),
                "pending": states.count("pending") + states.count("downloading"),
            }

    def progress(self) -> dict:
        c = self.counts()
        done = c["ok"] + c["skipped"] + c["failed"]
        return {
            "id": getattr(self, "job_id", None),
            "kind": self.kind,
            "status": self.status,
            "message": self.message,
            "name": self.entry_name,
            "collection_id": self.summary.get("id"),
            "folder": str(self.folder),
            "elapsed": round((self.finished_at or time.time()) - (self.started_at or self.created_at), 1),
            "bytes": self.bytes_done,
            "speed": round(self.speed_now()),
            "transfers": len(self.timeline),
            "errors": {str(k): v for k, v in list(self.errors.items())[:20]},
            **c,
            "done": done,
        }

    # -- persistence --------------------------------------------------------
    def save_state(self) -> None:
        payload = {
            "collection_id": self.summary.get("id"),
            "name": self.entry_name,
            "folder": str(self.folder),
            "status": self.status,
            "sets": {str(k): v for k, v in self.sets.items()},
            "errors": {str(k): v for k, v in self.errors.items()},
            "updated_at": time.time(),
        }
        try:
            (self.folder / "download-state.json").write_text(json.dumps(payload, indent=1), encoding="utf-8")
        except OSError:
            pass

    def load_state(self) -> None:
        """Adopt finished sets from a previous run — but only if the file is
        actually still on disk (lazer deletes archives it imports, so the state
        file alone is not trustworthy)."""
        p = self.folder / "download-state.json"
        if not p.exists():
            return
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return
        for k, v in (data.get("sets") or {}).items():
            try:
                sid = int(k)
            except (TypeError, ValueError):
                continue
            if sid not in self.sets or v not in ("ok", "skipped"):
                continue
            candidate = self.folder / f"{sid}.osz"
            try:
                if candidate.is_file() and candidate.stat().st_size >= 1024:
                    self.sets[sid] = v
            except OSError:
                continue


def _is_valid_osz(path: Path, verify: bool) -> tuple[bool, str]:
    try:
        if path.stat().st_size < 1024:
            return False, "file too small"
        with open(path, "rb") as fh:
            if fh.read(4) != b"PK\x03\x04":
                return False, "not a zip archive"
        if not verify:
            return True, ""
        with zipfile.ZipFile(path) as zf:
            if zf.testzip() is not None:
                return False, "corrupt archive (CRC mismatch)"
            if not any(n.lower().endswith(".osu") for n in zf.namelist()):
                return False, "no .osu file inside"
        return True, ""
    except zipfile.BadZipFile:
        return False, "bad zip file"
    except OSError as exc:
        return False, f"io error: {exc}"


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _replace_with_retry(src: Path, dst: Path, attempts: int = 6, delay: float = 0.5) -> bool:
    """Move a finished download into place.

    On Windows an antivirus scan (or the indexer) can briefly hold a handle on a
    freshly written archive, which makes the rename fail with WinError 32/5.
    Retry, then fall back to copy+delete, which needs read access only.
    """
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return True
        except PermissionError:
            time.sleep(delay * (i + 1))
        except OSError as exc:
            if getattr(exc, "winerror", None) in (5, 32):
                time.sleep(delay * (i + 1))
            else:
                raise
    try:
        shutil.copyfile(src, dst)
        _safe_unlink(src)
        return True
    except OSError:
        return False


def _download_once(
    pool: MirrorPool,
    sid: int,
    dest: Path,
    no_video: bool,
    job: DownloadJob,
    *,
    tolerant: bool = False,
    passes: int | None = None,
) -> tuple[str, str]:
    """Try mirrors until one yields a valid archive. Returns (mirror, error).

    `tolerant` disables the speed floor (used on the final pass so a genuinely
    slow but working mirror still gets the file in).
    """
    last_error = "no mirror succeeded"
    for _ in range(passes or MAX_PASSES_PER_SET):
        name = pool.acquire()
        url = pool.url_for(name, sid, no_video)
        tmp = dest.with_suffix(dest.suffix + ".part")
        written = 0
        try:
            transfer_started = time.monotonic()
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=STALL_TIMEOUT) as resp:
                ctype = (resp.headers.get("Content-Type") or "").lower()
                if resp.status not in (200, 206):
                    raise OSError(f"HTTP {resp.status}")
                if "text/html" in ctype or "application/json" in ctype:
                    raise OSError(f"unexpected content-type {ctype}")
                started = time.monotonic()
                with open(tmp, "wb") as fh:
                    while True:
                        chunk = resp.read(512 * 1024)
                        if not chunk:
                            break
                        fh.write(chunk)
                        written += len(chunk)
                        elapsed = time.monotonic() - started
                        if not tolerant and elapsed > SLOW_GRACE and written / elapsed < MIN_SPEED:
                            raise SlowTransfer(f"only {written / elapsed / 1024:.0f} KB/s after {elapsed:.0f}s")
            seconds = time.monotonic() - transfer_started
            ok, reason = _is_valid_osz(tmp, job.verify)
            if not ok:
                _safe_unlink(tmp)
                pool.report(name, False)
                last_error = f"{name}: {reason}"
                continue
            if not _replace_with_retry(tmp, dest):
                # local problem (file locked by a scanner) — not the mirror's fault
                _safe_unlink(tmp)
                last_error = "could not move the finished archive into place (locked by another program)"
                continue
            job.add_bytes(written)
            job.record_transfer(sid, name, seconds, written)
            pool.report(name, True, speed=written / seconds if seconds > 0 else None)
            return name, ""
        except urllib.error.HTTPError as exc:
            _safe_unlink(tmp)
            if exc.code == 404:
                # not on this mirror; do not cool it down
                pool.report(name, False, not_found=True)
                last_error = f"{name}: not carried by this mirror"
                continue
            pool.report(name, False)
            last_error = f"{name}: HTTP {exc.code}"
        except SlowTransfer as exc:
            _safe_unlink(tmp)
            pool.report(name, False)
            last_error = f"{name}: {exc}"
            job.add_bytes(written)
        except Exception as exc:  # timeouts, resets, disk issues
            _safe_unlink(tmp)
            pool.report(name, False)
            last_error = f"{name}: {type(exc).__name__}: {exc}"
            job.add_bytes(written)
    return "", last_error


class Downloader:
    def __init__(self, pool: MirrorPool, cancel: threading.Event | None = None):
        self.pool = pool
        self.cancel = cancel or threading.Event()

    def run(self, job: DownloadJob) -> DownloadJob:
        job.started_at = time.time()
        job.status = "running"
        job.load_state()
        job.folder.mkdir(parents=True, exist_ok=True)

        pending = [sid for sid, state in job.sets.items() if state not in ("ok", "skipped")]
        self._run_group(job, pending)

        # second wind: by now the pool knows which mirrors answer and how fast,
        # so transient failures get one more chance before being reported.
        failed = [sid for sid, state in job.sets.items() if state == "failed"]
        if failed and not self.cancel.is_set():
            for sid in failed:
                job.sets[sid] = "pending"
                job.errors.pop(sid, None)
            self._run_group(job, failed)

        if self.cancel.is_set():
            job.status = "cancelled"
            job.message = "cancelled"
        else:
            c = job.counts()
            job.status = "done" if c["failed"] == 0 else "partial"
            job.message = (
                f"{c['ok'] + c['skipped']}/{c['total']} beatmapsets in place"
                + (f", {c['failed']} failed" if c["failed"] else "")
            )
        job.finished_at = time.time()
        job.save_state()
        return job

    def _run_group(self, job: DownloadJob, sids: list[int]) -> None:
        if not sids:
            return
        with ThreadPoolExecutor(max_workers=job.concurrency) as pool_exec:
            futures = {}
            for sid in sids:
                dest = job.folder / f"{sid}.osz"
                if dest.exists():
                    ok, _ = _is_valid_osz(dest, job.verify)
                    if ok:
                        job.set_state(sid, "skipped")
                        continue
                    _safe_unlink(dest)
                job.set_state(sid, "downloading")
                futures[pool_exec.submit(self._worker, job, sid, dest)] = sid

            for future in futures:
                if self.cancel.is_set():
                    break
                try:
                    future.result()
                except Exception as exc:
                    job.set_state(futures[future], "failed", f"{type(exc).__name__}: {exc}")
                job.save_state()

    def _worker(self, job: DownloadJob, sid: int, dest: Path) -> None:
        if self.cancel.is_set():
            job.set_state(sid, "pending")
            return
        # give every enabled mirror a fair chance before giving up on a set
        passes = max(MAX_PASSES_PER_SET, len(self.pool.names))
        mirror, error = _download_once(self.pool, sid, dest, job.no_video, job, passes=passes)
        if not mirror and "KB/s" in error:
            # everything was too slow — accept whichever mirror actually finishes
            mirror2, error2 = _download_once(self.pool, sid, dest, job.no_video, job, passes=2, tolerant=True)
            mirror, error = (mirror2, "") if mirror2 else (mirror, error2 or error)
        if mirror:
            job.set_state(sid, "ok")
        else:
            job.set_state(sid, "failed", error)
