"""Checks for the direct beatmap import plumbing (no lazer install needed).

The helper itself is covered by the app's own runs; what matters here is the contract
around it: the list file it reads, the report it returns, and the refusal to run when
an archive is missing (better to fail before opening the realm than mid-import).
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import lazerdb  # noqa: E402

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
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        lists = root / "lists"

        # 1. the list file: absolute paths, one per line, trailing newline, utf-8
        archives = []
        for name in ("1.osz", "2.osz"):
            f = root / name
            f.write_bytes(b"PK\x03\x04")
            archives.append(f)
        list_file = lazerdb.import_list_file(archives, directory=lists)
        content = list_file.read_text(encoding="utf-8")
        lines = content.splitlines()
        check("list file has one absolute path per line", lines == [str(p.resolve()) for p in archives], lines)
        check("list file ends with a newline", content.endswith("\n"), repr(content[-3:]))
        check("list file lives in the given directory", list_file.parent == lists, list_file)

        # 2. a missing archive is refused before anything runs
        missing = [archives[0], root / "nope.osz"]
        try:
            lazerdb.import_beatmaps(missing, data_dir=root)
            check("missing archive refused", False, "no error raised")
        except RuntimeError as exc:
            check("missing archive refused", "missing" in str(exc), str(exc))

        # 3. the helper's report is mapped onto the app's shape, and the list file is cleaned up
        captured: dict = {}
        real_run = lazerdb._run
        real_status = lazerdb.version_status

        def fake_run(args, timeout=None):
            captured["args"] = args
            captured["timeout"] = timeout
            captured["list_exists"] = Path(args[args.index("--beatmaps-from") + 1]).is_file()
            return 0, {
                "beatmaps_requested": 3,
                "beatmaps_imported": 2,
                "beatmaps_failed": 1,
                "seconds": 1.5,
                "errors": ["3.osz: ArgumentException: No valid beatmap files found in the beatmap archive."],
                "failed_files": [str(root / "3.osz")],
                "beatmap_sets_before": 10,
                "beatmap_sets_after": 12,
            }, ""

        lazerdb.version_status = lambda *a, **k: {"available": True}  # type: ignore[assignment]
        lazerdb._run = fake_run  # type: ignore[assignment]
        third = root / "3.osz"
        third.write_bytes(b"PK\x03\x04")
        try:
            report = lazerdb.import_beatmaps([archives[0], archives[1], third], data_dir=root, parallel=4)
        finally:
            lazerdb._run = real_run  # type: ignore[assignment]
            lazerdb.version_status = real_status  # type: ignore[assignment]

        check("imported/failed counts come through", (report["imported"], report["failed"]) == (2, 1), report)
        check("seconds come through", report["seconds"] == 1.5, report)
        check("failed files are named so they are never deleted", report["failed_files"] == [str(third)], report)
        check("realm set counts come through", (report["sets_before"], report["sets_after"]) == (10, 12), report)
        check("--parallel is passed through", "--parallel" in captured["args"] and "4" in captured["args"], captured["args"])
        check("the list file existed while the helper ran", captured["list_exists"] is True)
        check("the list file is removed afterwards", not Path(captured["args"][captured["args"].index("--beatmaps-from") + 1]).exists())
        check("timeout scales with the batch size", (captured.get("timeout") or 0) > 60, captured.get("timeout"))

        # 4. an empty request is a no-op, not an error
        empty = lazerdb.import_beatmaps([], data_dir=root)
        check("empty request is a no-op", empty["imported"] == 0 and empty["failed"] == 0, empty)

    print(f"\n{CHECKS - len(FAILED)}/{CHECKS} checks passed")
    for name in FAILED:
        print(f"  failed: {name}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
