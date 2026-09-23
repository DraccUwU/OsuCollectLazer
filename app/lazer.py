"""osu!lazer detection and beatmap import.

Import path: a second `osu!.exe <file> ...` launch while lazer is already running
forwards every path to the running instance over lazer's IPC pipe
(`ArchiveImportIPCChannel`), which imports files whose extension it handles
(.osz/.olz/.osk/.osr). No window interaction required.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\osulazer"
EXE_NAME = "osu!.exe"


def _registry_values() -> dict:
    if os.name != "nt":
        return {}
    try:
        import winreg  # type: ignore
    except ImportError:
        return {}
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as key:
            out = {}
            i = 0
            while True:
                try:
                    name, value, _ = winreg.EnumValue(key, i)
                except OSError:
                    break
                out[name] = value
                i += 1
            return out
    except OSError:
        return {}


def find_lazer_exe(explicit: str | None = None) -> Path | None:
    """Locate osu!.exe (registry first, then the standard per-user install paths)."""
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    reg = _registry_values()
    icon = str(reg.get("DisplayIcon") or "")
    if icon:
        icon = icon.split(",")[0].strip().strip('"')
        if icon:
            candidates.append(Path(icon))
    install = str(reg.get("InstallLocation") or "")
    if install:
        candidates.append(Path(install) / "current" / EXE_NAME)
        candidates.append(Path(install) / EXE_NAME)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(Path(local) / "osulazer" / "current" / EXE_NAME)
        candidates.append(Path(local) / "osulazer" / EXE_NAME)
        candidates.append(Path(local) / "osu" / EXE_NAME)
    for c in candidates:
        try:
            if c.is_file():
                return c
        except OSError:
            continue
    return None


def lazer_version() -> str | None:
    reg = _registry_values()
    version = reg.get("DisplayVersion") or reg.get("DisplayName")
    if isinstance(version, str):
        m = re.search(r"\d{4}\.\d{3,4}\.\d", version)
        if m:
            return m.group(0)
        return version
    return None


def data_dir() -> Path | None:
    """lazer's data directory (client.realm, files/, logs/) as configured."""
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return None
    cfg = Path(appdata) / "osu" / "storage.ini"
    if cfg.is_file():
        try:
            for line in cfg.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.strip().lower().startswith("fullpath"):
                    _, _, value = line.partition("=")
                    candidate = Path(value.strip())
                    if candidate.is_dir():
                        return candidate
        except OSError:
            pass
    fallback = Path(appdata) / "osu"
    return fallback if fallback.is_dir() else None


def is_running() -> bool:
    if os.name != "nt":
        return False
    try:
        out = subprocess.run(
            ["tasklist", "/NH", "/FI", f"IMAGENAME eq {EXE_NAME}"],
            capture_output=True,
            text=True,
            timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
    except Exception:
        return False
    return EXE_NAME.lower() in out.lower()


def start_lazer(exe: Path) -> bool:
    if is_running():
        return True
    try:
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen([str(exe)], creationflags=flags, close_fds=True)
    except Exception:
        return False
    for _ in range(60):  # up to ~60s for the IPC pipe to come up
        if is_running():
            time.sleep(4)
            return True
        time.sleep(1)
    return is_running()


def stage_for_import(files: list[Path], staging_dir: Path) -> tuple[list[Path], str]:
    """Hardlink (or copy) files into a staging dir before handing them to lazer.

    lazer deletes archives it imports (`ShouldDeleteArchive`), so pushing the
    library copies directly would empty the download folder. A hardlink costs no
    extra space: lazer deletes its link, the library copy stays.
    """
    staging_dir.mkdir(parents=True, exist_ok=True)
    staged: list[Path] = []
    mode = "hardlink"
    for f in files:
        target = staging_dir / f.name
        try:
            if target.exists():
                target.unlink()
        except OSError:
            pass
        try:
            try:
                os.link(f, target)
            except OSError:
                mode = "copy"
                shutil.copyfile(f, target)
            staged.append(target)
        except OSError as exc:
            raise RuntimeError(f"could not stage {f.name}: {exc}") from exc
    return staged, mode


def clear_staging(staging_dir: Path) -> None:
    shutil.rmtree(staging_dir, ignore_errors=True)


def clear_staging_later(staging_dir: Path, attempts: int = 24, delay: float = 5.0) -> None:
    """Remove the staging dir once lazer has let go of the hardlinks.

    Lazer queues imports asynchronously, so right after a push its handles are
    still open and the first rmtree fails silently. Retry in the background.
    """
    for _ in range(attempts):
        if not staging_dir.exists():
            return
        shutil.rmtree(staging_dir, ignore_errors=True)
        if not staging_dir.exists():
            return
        time.sleep(delay)
    shutil.rmtree(staging_dir, ignore_errors=True)


def sweep_stale_staging(root: Path) -> int:
    """Delete `.import-staging` leftovers from previous runs."""
    removed = 0
    if not root.is_dir():
        return 0
    for candidate in root.glob("*/.import-staging"):
        clear_staging(candidate)
        if not candidate.exists():
            removed += 1
    return removed


def _batches(files: list[Path], batch_size: int, max_chars: int = 8000):
    batch: list[Path] = []
    length = 0
    for f in files:
        item = len(str(f)) + 3
        if batch and (len(batch) >= batch_size or length + item > max_chars):
            yield batch
            batch, length = [], 0
        batch.append(f)
        length += item
    if batch:
        yield batch


def hand_to_lazer(
    files: list[Path],
    exe: Path | None = None,
    *,
    batch_size: int = 20,
    parallel: int = 8,
    on_batch=None,
    on_progress=None,
    cancel=None,
    transport: str = "auto",
) -> dict:
    """Give .osz paths to a running lazer, fastest route first.

    `pipe`  — write straight to the game's IPC pipe (no process per batch; the
              launcher's ~1-2 s cold start is what capped import speed before)
    `launcher` — `osu!.exe <paths…>`, i.e. the old behaviour (also the fallback)

    Returns a report with `transport`, `pushed` (list), `failed`, `batches`,
    `seconds`, `errors` and — when it had to fall back — `reason`.
    """
    from . import ipc

    fallback_reason: str | None = None

    if transport in ("auto", "pipe"):
        ok, reason = ipc.available()
        if ok:
            try:
                result = ipc.send_paths(files, on_progress=on_progress, cancel=cancel)
                return {
                    "transport": "pipe",
                    "pushed": result["sent"],
                    "failed": result["failed"],
                    "batches": 1,
                    "seconds": result["seconds"],
                    "errors": result["errors"],
                    "reason": None,
                }
            except ipc.IpcStuck as exc:
                # the launcher forwards through the same pipe, so falling back would
                # only burn a 3s IPC timeout per file
                raise RuntimeError(str(exc)) from exc
            except ipc.IpcUnavailable as exc:
                if transport == "pipe":
                    raise RuntimeError(f"the game's IPC pipe is unavailable: {exc}") from exc
                fallback_reason = str(exc)
        else:
            if transport == "pipe":
                raise RuntimeError(f"the game's IPC pipe is unavailable: {reason}")
            fallback_reason = reason

    result = push_files(files, exe, batch_size=batch_size, parallel=parallel, on_batch=on_batch, cancel=cancel)
    result["transport"] = "launcher"
    result["reason"] = fallback_reason
    result.setdefault("seconds", 0.0)
    return result


def push_files(
    files: list[Path],
    exe: Path | None = None,
    *,
    batch_size: int = 20,
    timeout_per_batch: float = 240.0,
    parallel: int = 1,
    on_batch=None,
    cancel=None,
) -> dict:
    """Hand .osz files to a running lazer. Returns {pushed, failed, batches, errors}.

    Each launch of `osu!.exe <files…>` forwards every path through lazer's IPC channel,
    and lazer turns each forwarded path into its own import task — so a batch is a
    throughput knob, not a task count. `parallel` runs several launches at once (the
    forwarder's own cold start dominates otherwise).
    """
    exe = exe or find_lazer_exe()
    if not exe:
        raise RuntimeError("osu!lazer executable not found")
    if not is_running():
        raise RuntimeError("osu!lazer is not running (start it first)")

    batches = list(_batches(list(files), batch_size))
    pushed: list[str] = []
    failed: list[str] = []
    errors: list[str] = []

    def run_batch(number: int, batch: list[Path]) -> tuple[int, list[Path], set[str], str | None]:
        args = [str(exe), *[str(f) for f in batch]]
        seen: set[str] = set()
        error: str | None = None
        try:
            proc = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=timeout_per_batch,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            out = (proc.stdout or "") + (proc.stderr or "")
            for line in out.splitlines():
                m = re.match(r"Importing (.+)$", line.strip())
                if m:
                    seen.add(Path(m.group(1)).name)
            if proc.returncode != 0 and not seen:
                error = f"batch {number}: exit {proc.returncode} {out.strip()[:200]}"
        except subprocess.TimeoutExpired:
            error = f"batch {number}: timed out after {timeout_per_batch:.0f}s"
        except OSError as exc:
            error = f"batch {number}: {type(exc).__name__}: {exc}"
        return number, batch, seen, error

    def collect(number: int, batch: list[Path], seen: set[str], error: str | None) -> None:
        if error:
            errors.append(error)
        for f in batch:
            (pushed if f.name in seen else failed).append(f.name)
        if on_batch:
            on_batch(number, pushed, failed)

    if parallel > 1 and len(batches) > 1:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=max(1, parallel)) as pool:
            futures = [pool.submit(run_batch, n, b) for n, b in enumerate(batches, 1)]
            for future in futures:
                collect(*future.result())
                if cancel is not None and cancel.is_set():
                    break
    else:
        for number, batch in enumerate(batches, 1):
            if cancel is not None and cancel.is_set():
                break
            collect(*run_batch(number, batch))

    return {
        "pushed": pushed,
        "failed": failed,
        "batches": len(batches),
        "errors": errors[:10],
    }


def info(settings: dict | None = None) -> dict:
    exe = find_lazer_exe((settings or {}).get("lazer_exe") or None)
    return {
        "exe": str(exe) if exe else None,
        "version": lazer_version(),
        "running": is_running(),
        "data_dir": str(data_dir()) if data_dir() else None,
    }


if __name__ == "__main__":  # quick manual check: python -m app.lazer
    import json

    print(json.dumps(info(), indent=2))
    if len(sys.argv) > 1:
        print(push_files([Path(p) for p in sys.argv[1:]]))
