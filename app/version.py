"""Version and release coordinates, in one place.

The app, the first-run wizard and the release workflow all need the same names; the
asset names are asserted against `.github/workflows/release.yml` by `tests/test_setup.py`
so the wizard can never look for an asset the release does not publish.
"""
from __future__ import annotations

__version__ = "1.1.0"

REPO = "DraccUwU/OsuCollectLazer"
RELEASES_URL = f"https://github.com/{REPO}/releases"
LATEST_RELEASE_API = f"https://api.github.com/repos/{REPO}/releases/latest"

HELPER_ASSET = "LazerDb-win-x64.zip"  # the wizard downloads this on first run
APP_ASSET = "OsuCollectLazer-win-x64.zip"  # portable folder build
SETUP_ASSET = "OsuCollectLazer-Setup.exe"  # installer
SINGLE_ASSET = "OsuCollectLazer.exe"  # single-file build
