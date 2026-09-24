"""Entry point for the packaged app (PyInstaller).

A windowed build has no console, so `print()` would fail outright and a crash would
vanish: stdout/stderr go to `app.log` in the app data dir first, fatal errors surface in
a message box, and starting a second copy just opens the copy that is already running.
"""
from __future__ import annotations

import sys
import urllib.request
import webbrowser


def _log_stream():
    """Redirect stdout/stderr to app.log (a windowed build has neither)."""
    try:
        from app import config

        return open(config.app_dir() / "app.log", "a", encoding="utf-8", errors="replace", buffering=1)
    except Exception:
        return None


def _message_box(text: str) -> None:
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, text, "OsuCollectLazer", 0x10)
    except Exception:
        pass


def _cli_port(argv: list[str]) -> int | None:
    """`--port 9000` / `--port=9000` on the command line, so the running-copy probe
    checks the same port this copy would use."""
    for index, arg in enumerate(argv):
        if arg.startswith("--port="):
            value = arg.split("=", 1)[1]
        elif arg == "--port" and index + 1 < len(argv):
            value = argv[index + 1]
        else:
            continue
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _running_url(port: int) -> str | None:
    url = f"http://127.0.0.1:{port}/api/status"
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            if response.status == 200:
                return url.rsplit("/api/status", 1)[0] + "/"
    except Exception:
        return None
    return None


def main() -> int:
    stream = _log_stream()
    if stream is not None:
        sys.stdout = stream
        sys.stderr = stream

    try:
        from app import config, server, version
    except Exception as exc:  # pragma: no cover - only reachable in a broken bundle
        _message_box(f"OsuCollectLazer could not start:\n\n{type(exc).__name__}: {exc}")
        return 1

    print(f"\n--- OsuCollectLazer {version.__version__} ---")
    port = _cli_port(sys.argv[1:]) or int(config.load_settings().get("port", 8765))
    already = _running_url(port)
    if already:
        print(f"already running at {already} — opening it")
        webbrowser.open(already)
        return 0

    try:
        code = server.main(sys.argv[1:])
    except Exception as exc:
        print(f"fatal: {type(exc).__name__}: {exc}")
        _message_box(
            f"OsuCollectLazer stopped:\n\n{type(exc).__name__}: {exc}\n\n"
            "app.log next to your settings has the details."
        )
        return 1
    if code != 0:
        # could not bind: a running copy is the usual reason — show that one instead
        webbrowser.open(f"http://127.0.0.1:{port}/")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
