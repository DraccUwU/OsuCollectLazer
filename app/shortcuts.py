"""Desktop and Start-menu shortcuts, so the packaged app installs like a real one.

Deliberately dependency-free: the .lnk is written through WScript.Shell from PowerShell
(which every Windows has), and the two shell folders are *asked for* through .NET rather
than guessed from %USERPROFILE% — desktops are commonly redirected into OneDrive, and the
Start menu is localised.

The packaged build points a shortcut at its own exe; a source checkout points it at
start.bat, which is handy while developing.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

NAME = "OsuCollectLazer"
DESCRIPTION = "osu!collector collections into osu!lazer"
PLACES = ("desktop", "startmenu")
_LABEL = {"desktop": "desktop", "startmenu": "start menu"}


def shortcut_target() -> Path | None:
    """What a shortcut should launch: the frozen exe, or start.bat from a checkout."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve()
    launcher = Path(__file__).resolve().parent.parent / "start.bat"
    return launcher if launcher.exists() else None


def _icon_for(target: Path) -> Path:
    """An exe carries its own icon; a .bat needs the one the app ships."""
    return target if target.suffix.lower() == ".exe" else Path(__file__).resolve().parent / "web" / "favicon.ico"


def _powershell(script: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _quote(value: object) -> str:
    """A PowerShell single-quoted literal."""
    return "'" + str(value).replace("'", "''") + "'"


def places() -> dict[str, Path]:
    """Where the two shortcuts go, as Windows itself reports the folders."""
    script = (
        "$desktop  = [Environment]::GetFolderPath('Desktop')\n"
        "$programs = [Environment]::GetFolderPath('Programs')\n"
        "Write-Output (ConvertTo-Json -Compress @{ desktop = $desktop; startmenu = $programs })"
    )
    proc = _powershell(script, timeout=30)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "could not ask Windows for the shell folders").strip()[:200])
    data = json.loads(proc.stdout.strip().splitlines()[-1])
    return {"desktop": Path(data["desktop"]) / f"{NAME}.lnk", "startmenu": Path(data["startmenu"]) / f"{NAME}.lnk"}


def status() -> dict:
    """What the UI shows: whether shortcuts can be made, and which exist."""
    target = shortcut_target()
    payload: dict = {
        "available": target is not None,
        "reason": None if target else "shortcuts need the packaged app",
        "target": str(target) if target else None,
        "places": {},
    }
    if target is None:
        return payload
    try:
        payload["places"] = {
            where: {"path": str(path), "exists": path.exists(), "label": _LABEL[where]}
            for where, path in places().items()
        }
    except Exception as exc:  # PowerShell missing or refusing: say so, do not crash the UI
        payload["available"] = False
        payload["reason"] = f"{type(exc).__name__}: {exc}"
    return payload


def write_shortcut(path: Path, target: Path, description: str = DESCRIPTION) -> None:
    """Write one .lnk (the piece that talks to Windows)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    script = (
        "$shell = New-Object -ComObject WScript.Shell\n"
        f"$lnk = $shell.CreateShortcut({_quote(path)})\n"
        f"$lnk.TargetPath = {_quote(target)}\n"
        f"$lnk.WorkingDirectory = {_quote(target.parent)}\n"
        f"$lnk.IconLocation = {_quote(_icon_for(target))}\n"
        f"$lnk.Description = {_quote(description)}\n"
        "$lnk.Save()\n"
    )
    proc = _powershell(script)
    if proc.returncode != 0 or not path.exists():
        detail = (proc.stderr or proc.stdout or "the shortcut was not written").strip()
        raise RuntimeError(detail[:300])


def create(where: str) -> dict:
    if where not in PLACES:
        raise ValueError(f"where must be one of: {', '.join(PLACES)}")
    target = shortcut_target()
    if target is None:
        raise RuntimeError("no exe to point at — shortcuts only work in the packaged app")
    path = places()[where]
    write_shortcut(path, target)
    return {"where": where, "label": _LABEL[where], "path": str(path), "target": str(target), "created": True}


def remove(where: str) -> dict:
    if where not in PLACES:
        raise ValueError(f"where must be one of: {', '.join(PLACES)}")
    path = places()[where]
    existed = path.exists()
    if existed:
        path.unlink()
    return {"where": where, "label": _LABEL[where], "path": str(path), "removed": existed}
