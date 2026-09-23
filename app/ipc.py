"""Hand files to a running osu!lazer by talking to its IPC pipe directly.

`osu!.exe <file> …` is a whole osu! frontend cold start (velopack bootstrap, .NET init,
pipe connect, one message per file, exit) — roughly 1–2 s per launch no matter how many
paths you pass it, which is what capped import speed. The game itself listens on a named
pipe and does not care who writes to it:

    pipe   \\\\.\\pipe\\osu-framework-osu-lazer
           (osu.Framework's NamedPipeIpcProvider prefixes the game's `osu-lazer`)
    frame  [int32 little-endian length][UTF-8 JSON]
    JSON   {"Type": "<assembly-qualified message type>", "Value": {"Path": "<file>"}}

The Type string is compared verbatim by the game (version included), so it is read from
the installed osu.Game.dll (see lazerdb.ipc_message_type) rather than hardcoded.

One connection per message, matching what the framework's own client does; the server
accepts a single connection at a time, so sends are sequential (and still ~1 ms each).
"""
from __future__ import annotations

import json
import struct
import time
from pathlib import Path

PIPE_PATH = r"\\.\pipe\osu-framework-osu-lazer"
ERROR_PIPE_BUSY = 231
CONNECT_TIMEOUT = 5.0
BUSY_SLEEP = 0.02


class IpcUnavailable(RuntimeError):
    """The game's IPC pipe can't be used (not running, or protocol/dll mismatch)."""


class IpcStuck(IpcUnavailable):
    """The game's pipe exists but never became free — its listener is wedged.

    The game binds a **single** pipe instance and accepts one connection at a time
    (`new NamedPipeServerStream(name, PipeDirection.InOut, 1)`), so a connection that
    is opened and never completed — e.g. a "is it listening?" probe that just connects
    and closes — can leave the listener unusable until osu!lazer is restarted. Every
    send therefore has to be a complete frame, and there is no connect-only probe.
    """


def frame_for(path: Path, type_string: str) -> bytes:
    payload = json.dumps(
        {"Type": type_string, "Value": {"Path": str(Path(path))}},
        separators=(",", ":"),
    ).encode("utf-8")
    return struct.pack("<i", len(payload)) + payload


def _connect(timeout: float = CONNECT_TIMEOUT):
    """Open one connection to the game's pipe, waiting out ERROR_PIPE_BUSY.

    Never returns a handle without the caller writing a frame to it (see IpcStuck).
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            # named pipes open like files; buffering=0 keeps the frame in one write
            return open(PIPE_PATH, "r+b", buffering=0)
        except FileNotFoundError as exc:
            raise IpcUnavailable("the game's IPC pipe isn't there (is osu!lazer running?)") from exc
        except OSError as exc:
            if getattr(exc, "winerror", None) == ERROR_PIPE_BUSY:
                if time.monotonic() < deadline:
                    time.sleep(BUSY_SLEEP)
                    continue
                raise IpcStuck(
                    f"the game's IPC pipe has been busy for {timeout:.0f}s — its listener is stuck, "
                    "restart osu!lazer to clear it"
                ) from exc
            raise IpcUnavailable(f"{type(exc).__name__}: {exc}") from exc


def available(timeout: float = 3.0) -> tuple[bool, str]:
    """Whether the pipe route should be attempted at all.

    Deliberately does **not** connect: a connect that isn't followed by a full frame
    is what wedges the game's single-instance listener (see IpcStuck). A missing or
    stuck pipe shows up on the first send instead.
    """
    from . import lazer

    if not lazer.is_running():
        return False, "osu!lazer is not running"
    return True, "ok"


def send_paths(
    paths,
    *,
    type_string: str | None = None,
    on_progress=None,
    cancel=None,
    retries: int = 3,
    busy_timeout: float = 10.0,
) -> dict:
    """Send one import message per path. Returns sent/failed/errors/seconds.

    Raises IpcStuck as soon as the pipe stays busy past `busy_timeout` (nothing is
    gained by retrying: the listener needs a game restart).
    """
    from . import lazerdb

    type_string = type_string or lazerdb.ipc_message_type()
    sent: list[str] = []
    failed: list[str] = []
    errors: list[str] = []
    started = time.monotonic()

    for path in paths:
        if cancel is not None and cancel.is_set():
            break
        target = Path(path).resolve()
        frame = frame_for(target, type_string)
        error: str | None = None
        for _ in range(max(1, retries)):
            try:
                with _connect(busy_timeout) as pipe:
                    pipe.write(frame)
                error = None
                break
            except IpcStuck:
                raise
            except IpcUnavailable as exc:
                error = str(exc)
                time.sleep(BUSY_SLEEP)
        if error is None:
            sent.append(target.name)
        else:
            failed.append(target.name)
            errors.append(f"{target.name}: {error}")
        if on_progress:
            on_progress(len(sent), len(failed))

    return {
        "sent": sent,
        "failed": failed,
        "errors": errors[:10],
        "seconds": time.monotonic() - started,
    }


if __name__ == "__main__":
    import sys

    from . import lazer, lazerdb

    ok, reason = available()
    print(f"pipe available: {ok} ({reason})")
    print(f"message type  : {lazerdb.ipc_message_type()}")
    print(f"lazer running : {lazer.is_running()}")
    files = [Path(p) for p in sys.argv[1:]]
    if files:
        result = send_paths(files)
        rate = len(result["sent"]) / result["seconds"] if result["seconds"] else 0
        print(
            f"sent {len(result['sent'])}/{len(files)} in {result['seconds']:.2f}s "
            f"({rate:.0f} maps/s), failed: {result['failed']}"
        )
        for line in result["errors"]:
            print("   ", line)
