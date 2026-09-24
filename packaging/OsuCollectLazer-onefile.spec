# PyInstaller spec for the single-file build — one OsuCollectLazer.exe, no folder.
#
#   python -m PyInstaller --noconfirm --distpath dist-single packaging/OsuCollectLazer-onefile.spec
#
# Same program as the folder build (packaging/OsuCollectLazer.spec); the difference is
# that everything is packed into the exe and unpacked into a temp directory at each
# start, which costs a few seconds on launch and re-does it every time. That is the
# trade for handing someone a single file instead of a zip.
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

# no COLLECT: binaries and data go into the exe itself, which is what makes it one file
exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=str(ROOT / "app" / "web" / "favicon.ico"),
    version=str(ROOT / "packaging" / "version_info.txt"),
)
