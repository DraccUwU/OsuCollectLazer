"""First-run setup: what a fresh install is missing, and how to fix it.

The wizard in the web UI drives everything through here. It looks at the machine
(osu!lazer, the import helper, a .NET SDK), can fetch the published helper from the
latest GitHub release so an install never needs the SDK, and writes the finished
settings. Nothing here touches lazer's own data: the wizard only *finds* things and
installs the helper next to the app.
"""
from __future__ import annotations

import io
import json
import shutil
import subprocess
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

from . import config, lazer, lazerdb, version

USER_AGENT = f"OsuCollectLazer/{version.__version__}"
API_TIMEOUT = 60
DOWNLOAD_TIMEOUT = 600
BUILD_TIMEOUT = 1800


def _no_window() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _dotnet() -> tuple[str | None, str | None]:
    """(executable, version) of the .NET SDK, if the machine has one."""
    exe = shutil.which("dotnet")
    if not exe:
        return None, None
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=API_TIMEOUT, creationflags=_no_window())
        return exe, (out.stdout or "").strip() or None
    except Exception:
        return exe, None


def _helper_status() -> dict:
    helper = lazerdb.helper_path()
    if not helper:
        return {"path": None, "available": False, "reason": "the import helper is not installed yet"}
    return {"path": str(helper), **lazerdb.version_status()}


def state() -> dict:
    """Everything the wizard needs to render itself (and to decide to appear at all)."""
    settings = config.load_settings()
    dotnet_exe, dotnet_version = _dotnet()
    sources = (config.bundle_dir() / "tools" / "LazerDb" / "LazerDb.csproj").is_file()
    return {
        "version": version.__version__,
        "setup_complete": bool(settings.get("setup_complete")),
        "lazer": lazer.info(settings),
        "helper": {**_helper_status(), "install_dir": str(lazerdb.helper_install_dir())},
        "helper_asset": version.HELPER_ASSET,
        "releases_url": version.RELEASES_URL,
        "dotnet": {"path": dotnet_exe, "version": dotnet_version, "can_build": bool(dotnet_exe and sources)},
        "settings": settings,
    }


# ---------------------------------------------------------------- native pickers
def pick_path(kind: str = "folder", start: str | None = None) -> str | None:
    """A native picker via tkinter (stdlib), run in the server process.

    Returns None when there is no usable display or no tkinter — the wizard then just
    lets the path be typed instead.
    """
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception:
        return None
    root = None
    try:
        root = tk.Tk()
        root.withdraw()
        try:
            root.attributes("-topmost", True)
        except Exception:
            pass
        initial = start or str(Path.home())
        if kind == "file":
            chosen = filedialog.askopenfilename(
                initialdir=initial,
                title="Where is osu!.exe?",
                filetypes=[("osu!lazer", "osu!.exe"), ("Programs", "*.exe"), ("All files", "*.*")],
            )
        else:
            chosen = filedialog.askdirectory(initialdir=initial, title="Choose the download folder")
        return chosen or None
    except Exception:
        return None
    finally:
        try:
            if root is not None:
                root.destroy()
        except Exception:
            pass


# ---------------------------------------------------------------- the import helper
def _release_asset() -> tuple[str | None, str | None, str]:
    """(download url, release tag, error) for the helper asset of the latest release."""
    request = urllib.request.Request(
        version.LATEST_RELEASE_API,
        headers={"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(request, timeout=API_TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None, None, "there is no published release yet — build the helper from source instead"
        return None, None, f"GitHub answered HTTP {exc.code}"
    except Exception as exc:
        return None, None, f"could not reach GitHub ({type(exc).__name__}: {exc})"
    tag = payload.get("tag_name")
    for asset in payload.get("assets") or []:
        if asset.get("name") == version.HELPER_ASSET:
            return asset.get("browser_download_url"), tag, ""
    return None, tag, f"release {tag} has no {version.HELPER_ASSET} asset"


def download_helper() -> dict:
    """Fetch the published helper and unpack it where the app looks for it."""
    url, tag, error = _release_asset()
    if not url:
        raise RuntimeError(error)
    log = [f"release {tag}", f"asset {version.HELPER_ASSET}"]
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT) as response:
        blob = response.read()
    log.append(f"downloaded {len(blob) / 1_048_576:.1f} MB")
    try:
        archive = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile as exc:
        raise RuntimeError(f"the downloaded file is not a zip ({exc})") from exc
    names = archive.namelist()
    if not any(name.lower().endswith("lazerdb.exe") for name in names):
        raise RuntimeError("the downloaded zip has no LazerDb.exe in it")
    target = lazerdb.helper_install_dir()
    target.mkdir(parents=True, exist_ok=True)
    archive.extractall(target)
    log.append(f"unpacked {len(names)} file(s) into {target}")
    return {"method": "download", "tag": tag, "path": str(target), "log": log}


def build_helper() -> dict:
    """Compile the helper from source — only possible in a checkout, and needs the SDK."""
    exe, _ = _dotnet()
    if not exe:
        raise RuntimeError("the .NET SDK is not installed (https://dotnet.microsoft.com/download)")
    project = config.bundle_dir() / "tools" / "LazerDb" / "LazerDb.csproj"
    if not project.is_file():
        raise RuntimeError("this build ships no helper sources — download the helper instead")
    log = [f"dotnet build {project.name}"]
    # the project path is passed absolutely: a bare name would only resolve if the cwd
    # happened to be the folder above it
    proc = subprocess.run(
        [exe, "build", str(project), "-c", "Release"],
        cwd=str(project.parent),
        capture_output=True,
        text=True,
        timeout=BUILD_TIMEOUT,
        creationflags=_no_window(),
    )
    tail = [line.strip() for line in (proc.stdout or "").splitlines() if line.strip()]
    if proc.returncode != 0:
        detail = [line.strip() for line in (proc.stderr or proc.stdout or "").splitlines() if line.strip()]
        raise RuntimeError(detail[-1] if detail else "dotnet build failed")
    log += tail[-2:] if tail else []
    built = lazerdb.helper_path()
    if not built:
        raise RuntimeError("the build finished but no LazerDb.exe appeared")
    return {"method": "build", "tag": None, "path": str(built), "log": log}


def install_helper(method: str = "auto") -> dict:
    """`download` | `build` | `auto` (download, then fall back to building)."""
    if method == "download":
        result = download_helper()
    elif method == "build":
        result = build_helper()
    elif method == "auto":
        try:
            result = download_helper()
        except Exception as exc:
            log = [f"download failed: {exc}"]
            try:
                result = build_helper()
            except Exception as build_exc:
                raise RuntimeError(f"{exc} — and building it from source failed too: {build_exc}") from build_exc
            result["log"] = log + result["log"]
    else:
        raise ValueError(f"unknown method {method!r}")
    result["status"] = _helper_status()
    return result


# ---------------------------------------------------------------- finishing up
def apply_settings(patch: dict) -> dict:
    """Save the wizard's choices and mark the install as set up."""
    current = config.load_settings()
    patch = patch or {}
    for key, value in patch.items():
        if key not in config.DEFAULTS:
            continue
        if key == "download_dir" and value:
            # create it now so a typo shows up in the wizard, not on the first download
            value = str(config.download_dir({"download_dir": str(value)}))
        current[key] = value
    current["setup_complete"] = True
    config.save_settings(current)
    return current
