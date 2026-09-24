# PyInstaller spec for the packaged app.
#
#   python -m PyInstaller --noconfirm packaging/OsuCollectLazer.spec
#
# CI runs exactly this and zips dist/OsuCollectLazer/ (see .github/workflows/release.yml).
# The result needs no Python: it starts the local server, opens the browser, and the
# first-run wizard fetches the import helper from the matching release.
from pathlib import Path

ROOT = Path(SPECPATH).parent  # noqa: F821 - SPECPATH is provided by PyInstaller
NAME = "OsuCollectLazer"

a = Analysis(  # noqa: F821
    [str(ROOT / "app" / "launcher.py")],
    pathex=[str(ROOT)],
    binaries=[],
    # the web UI is read at runtime from the bundle (config.resource_dir)
    datas=[(str(ROOT / "app" / "web"), "app/web")],
    # imported inside functions, so name them explicitly: the folder picker, and the
    # window (pywebview + its WebView2 backend through pythonnet)
    hiddenimports=[
        "tkinter",
        "tkinter.filedialog",
        "webview",
        "webview.platforms.edgechromium",
        "clr_loader",
        "pythonnet",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["unittest", "pydoc", "doctest", "pdb", "test"],
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # no console window: the launcher writes app.log and shows a message box on failure
    console=False,
    disable_windowed_traceback=False,
    icon=str(ROOT / "app" / "web" / "favicon.ico"),
    version=str(ROOT / "packaging" / "version_info.txt"),
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name=NAME,
)
