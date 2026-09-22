"""Checks for the lazer import-log watcher.

The watcher decides when it is safe to delete the downloaded archives, so it has to
cope with the game writing a *new* log file per run — the case that made a real
import look like it never happened.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import importlog  # noqa: E402

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


def make_data_dir(root: Path, name: str, body: str, *, age: float = 0.0) -> Path:
    logs = root / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    path = logs / name
    path.write_text(body, encoding="utf-8")
    if age:
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))
    return path


def main() -> int:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)

        # 1. a marker sees confirmations appended to its own file
        log = make_data_dir(root, "100.database.log", "starting\n")
        marker = importlog.position(root)
        check("marker points at the newest log", Path(marker["path"]).name == "100.database.log")
        with open(log, "a", encoding="utf-8") as fh:
            fh.write("[abc] Import successfully completed!\n")
            fh.write("[def] Found existing beatmap for X – skipping import.\n")
            fh.write("[ghi] No content found in beatmap archive\n")
        state = importlog.read_since(marker)
        check("counts successes and skips", state["ok"] == 2, state)
        check("counts failures separately", state["failed"] == 1, state)

        # 2. the game restarts and writes a NEW log file: everything in it is new
        time.sleep(0.02)
        make_data_dir(root, "200.database.log", "")
        fresh = importlog.position(root)
        check("new run becomes the newest log", Path(fresh["path"]).name == "200.database.log")
        marker = importlog.position(root)
        # marker taken on the new file, then another even newer one appears
        time.sleep(0.02)
        newer = make_data_dir(root, "300.database.log", "[xyz] Import successfully completed!\n")
        state = importlog.read_since(marker)
        check("follows to a newer log file", Path(state["path"]).name == "300.database.log", state)
        check("counts the newer file from the start", state["ok"] == 1, state)

        # 3. appending to the newer file keeps accumulating from where it stopped
        with open(newer, "a", encoding="utf-8") as fh:
            fh.write("[lmn] Import successfully completed!\n")
        state2 = importlog.read_since(state)
        check("does not double-count", state2["ok"] == 2, state2)

        # 4. a log rotated away (smaller than the marker offset) is re-read from 0
        rotated = importlog.position(root)
        Path(rotated["path"]).write_text("[z] Import successfully completed!\n", encoding="utf-8")
        state3 = importlog.read_since(rotated)
        check("re-reads a truncated log", state3["ok"] == 1, state3)

        # 5. no logs at all does not explode
        empty = Path(tmp) / "nologs"
        empty.mkdir()
        blank = importlog.position(empty)
        check("missing logs produce an empty marker", blank["path"] is None and blank["ok"] == 0, blank)
        check("read_since on an empty marker is a no-op", importlog.read_since(blank)["ok"] == 0)

        # 6. wait_for_log_after returns immediately when a newer file already exists
        started = time.time() - 5
        got = importlog.wait_for_log_after(started, root, timeout=0.5)
        check("wait_for_log_after picks the newest", Path(got["path"]).name == "300.database.log", got)
        # and gives up cleanly when nothing is newer
        got2 = importlog.wait_for_log_after(time.time() + 60, root, timeout=0.3)
        check("wait_for_log_after times out cleanly", got2["path"] is not None)

    print(f"\n{CHECKS - len(FAILED)}/{CHECKS} checks passed")
    for name in FAILED:
        print(f"  failed: {name}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
