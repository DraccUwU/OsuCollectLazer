"""Entry point for the packaged app (PyInstaller).

`OsuCollectLazer.exe` starts the local server in a thread and opens it in a real window
(Edge WebView2 through pywebview). It is deliberately forgiving: with no pywebview, no
WebView2 runtime, or with `--browser`, it opens the default browser instead, so the app
always starts.

A windowed build has no console either, so stdout/stderr go to app.log first, fatal
errors surface in a message box, and a second copy opens the copy that is already
running rather than fighting over the port.
"""
from __future__ import annotations

import sys
import threading
import urllib.request
import webbrowser

WINDOW_TITLE = "OsuCollectLazer"
WINDOW_SIZE = (1180, 800)
WINDOW_MIN = (940, 620)
# matches the UI's own background, so opening the window does not flash white
WINDOW_BG = "#0d1116"


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

        ctypes.windll.user32.MessageBoxW(None, text, WINDOW_TITLE, 0x10)
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


class _JsApi:
    """Just enough for the page to tell it is inside the app window (see app.js)."""

    def app_window(self) -> bool:
        return True


def _dark_title_bar() -> None:
    """Windows 11 paints a light title bar unless the app asks for the dark one.

    Runs on pywebview's startup thread, because the window has to exist before it can be
    found; harmless when the API is missing (older Windows, no DWM).
    """
    import ctypes
    import time

    DWMWA_USE_IMMERSIVE_DARK_MODE = 20
    for _ in range(60):
        hwnd = ctypes.windll.user32.FindWindowW(None, WINDOW_TITLE)
        if hwnd:
            value = ctypes.c_int(1)
            try:
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE, ctypes.byref(value), ctypes.sizeof(value)
                )
            except Exception:
                pass
            return
        time.sleep(0.1)


def _open_window(url: str) -> bool:
    """Show `url` in a native window; False when that is not possible."""
    try:
        import webview
    except Exception as exc:
        print(f"no window toolkit ({type(exc).__name__}: {exc}) — using the browser")
        return False

    created = False
    try:
        webview.create_window(
            WINDOW_TITLE,
            url,
            js_api=_JsApi(),
            width=WINDOW_SIZE[0],
            height=WINDOW_SIZE[1],
            min_size=WINDOW_MIN,
            background_color=WINDOW_BG,
            text_select=True,
        )
        created = True
        webview.start(_dark_title_bar)  # the callback gets its own thread; returns on close
        return True
    except Exception as exc:
        print(f"window failed ({type(exc).__name__}: {exc}) — using the browser")
        return created  # a window may already be up: do not open a browser on top of it


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

    argv = sys.argv[1:]
    port = _cli_port(argv) or int(config.load_settings().get("port", 8765))
    url = f"http://127.0.0.1:{port}/"
    print(f"\n--- OsuCollectLazer {version.__version__} ---")

    running = _running_url(port)
    if running:
        print(f"already running at {running}")
        if "--browser" in argv or not _open_window(running):
            webbrowser.open(running)
        return 0

    if "--browser" in argv:
        return server.main(argv)  # the plain flow: serve, hand the URL to the browser

    httpd = server.bind(port)
    if httpd is None:
        print(f"could not bind {url} — is another copy running?")
        webbrowser.open(url)
        return 1

    http = threading.Thread(target=httpd.serve_forever, name="ocl-http", daemon=True)
    http.start()
    print(f"serving {url} (closing the window stops the app)")

    try:
        if not _open_window(url):
            webbrowser.open(url)
            http.join()  # no window possible: keep serving until Ctrl+C
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        print(f"fatal: {type(exc).__name__}: {exc}")
        _message_box(
            f"OsuCollectLazer stopped:\n\n{type(exc).__name__}: {exc}\n\n"
            "app.log next to your settings has the details."
        )
        return 1
    finally:
        httpd.shutdown()
        httpd.server_close()
    print("bye")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
