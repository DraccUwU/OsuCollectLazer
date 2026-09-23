"""Checks for the direct IPC pipe client (no game required).

The framing is what makes this work at all: `[int32 length][UTF-8 JSON]` with the
message type string the installed lazer compares verbatim. Getting either wrong means
the game silently ignores the import, so both are pinned here.
"""
from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import ipc  # noqa: E402

CHECKS = 0
FAILED: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    if condition:
        print(f"  ok   {label}")
    else:
        FAILED.append(label)
        print(f"  FAIL {label} {detail}")


def main() -> int:
    # 1. frame layout: 4-byte little-endian length followed by exactly that many bytes
    frame = ipc.frame_for(Path(r"C:\maps\12345.osz"), "Type.Name, Asm, Version=1.2.3.4")
    (length,) = struct.unpack("<i", frame[:4])
    check("length prefix is little-endian int32", length == len(frame) - 4, length)
    payload = json.loads(frame[4:].decode("utf-8"))
    check("payload is JSON with Type/Value",
          payload.get("Type") == "Type.Name, Asm, Version=1.2.3.4" and "Path" in payload.get("Value", {}),
          payload)
    check("path is passed through verbatim", payload["Value"]["Path"] == r"C:\maps\12345.osz", payload)

    # 2. no whitespace padding (Newtonsoft's Formatting.None on the other side):
    #    re-serialising compactly must reproduce the payload byte for byte
    compact = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    check("frame JSON is compact (byte-identical to separators=(\",\", \":\"))",
          compact == frame[4:], frame[4:120])

    # 3. non-ascii paths survive as UTF-8
    weird = ipc.frame_for(Path("C:/maps/曲.osz"), "T")
    (n,) = struct.unpack("<i", weird[:4])
    check("utf-8 path round-trips", json.loads(weird[4:4 + n].decode("utf-8"))["Value"]["Path"].endswith("曲.osz"))

    # 4. the message type fallback composes a 4-part assembly version
    composed = None
    original_info = None
    try:
        from app import lazer, lazerdb

        original_info = lazer.info
        lazer.info = lambda *a, **k: {"version": "2026.921.0"}  # type: ignore[assignment]
        lazerdb._IPC_TYPE_CACHE["value"] = None
        original_helper = lazerdb.helper_path
        lazerdb.helper_path = lambda: None  # force the fallback path
        composed = lazerdb.ipc_message_type()
    except Exception as exc:
        check("message type fallback runs", False, exc)
    finally:
        if original_info is not None:
            from app import lazer, lazerdb

            lazer.info = original_info  # type: ignore[assignment]
            lazerdb.helper_path = original_helper  # type: ignore[assignment]
            lazerdb._IPC_TYPE_CACHE["value"] = None
    if composed:
        check(
            "fallback type name matches the runtime format",
            composed == "osu.Game.IPC.ArchiveImportMessage, osu.Game, Version=2026.921.0.0, Culture=neutral, PublicKeyToken=null",
            composed,
        )

    # 5. availability never connects (connecting without a frame wedges the game)
    src = Path(ipc.__file__).read_text(encoding="utf-8")
    body = src.split("def available(", 1)[1].split("\ndef ", 1)[0]
    check("available() does not open the pipe", "_connect(" not in body and "open(" not in body, body[:200])
    check("IpcStuck exists for the wedged case", issubclass(ipc.IpcStuck, ipc.IpcUnavailable))

    print(f"\n{CHECKS - len(FAILED)}/{CHECKS} checks passed")
    for name in FAILED:
        print(f"  failed: {name}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
