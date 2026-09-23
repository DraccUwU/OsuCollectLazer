"""osu!lazer detection, data directory lookup, and closing the game for imports.

Maps do **not** go through the running game any more. The helper (`tools/LazerDb`, driven
by `app/lazerdb.py`) writes into lazer's file store and realm directly through lazer's own
importer, in parallel — several times faster than feeding the game one path at a time, but
it needs the game closed (one writer per realm). So the only thing this module ever does
*to* the game is shut it down cleanly when an import is about to start.
"""
from __future__ import annotations

import os
import re
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


def _wait_gone(timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_running():
            return True
        time.sleep(1.0)
    return not is_running()


def _taskkill(force: bool = False) -> None:
    cmd = ["taskkill", "/IM", EXE_NAME] + (["/F"] if force else [])
    try:
        subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        pass


def close_lazer(graceful: float = 12.0, forced: float = 15.0) -> tuple[bool, str]:
    """Close the game so the helper can write to its files and database.

    First a graceful request (`taskkill` without `/F` — what clicking the X does), waiting
    up to `graceful` seconds. A busy game (a library scan, an import, a beatmap being
    played) can ignore that for minutes, so if it is still there afterwards it is
    terminated outright; the caller is expected to tell the user, because anything
    unsaved in the game is gone. Returns (closed, how).
    """
    if not is_running():
        return True, "not running"
    _taskkill(force=False)
    if _wait_gone(graceful):
        return True, "closed"
    _taskkill(force=True)
    if _wait_gone(forced):
        return True, "force-closed"
    return False, f"still running after a graceful and a forced close (~{graceful + forced:.0f}s) — close it by hand, then import again"


def info(settings: dict | None = None) -> dict:
    exe = find_lazer_exe((settings or {}).get("lazer_exe") or None)
    return {
        "exe": str(exe) if exe else None,
        "version": lazer_version(),
        "running": is_running(),
        "data_dir": str(data_dir()) if data_dir() else None,
    }


if __name__ == "__main__":  # quick manual check: python -m app.lazer [--close]
    import json

    if "--close" in sys.argv:
        ok, reason = close_lazer()
        print(f"close: {ok} ({reason})")
    print(json.dumps(info(), indent=2))
