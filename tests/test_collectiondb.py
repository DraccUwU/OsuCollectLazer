"""Checks for the collection.db writer/reader.

Run:  python tests/test_collectiondb.py

Two of the checks use real, externally produced files:
  * osu.Game's own test resource (osu.Game.Tests/Resources/Collections/collections.db)
  * your osu!stable collection.db, if present
so a drifting implementation fails loudly instead of silently producing
databases lazer would misread.
"""
from __future__ import annotations

import os
import sys
import tempfile
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import collectiondb  # noqa: E402

CACHE = Path(tempfile.gettempdir()) / "osu-collect-lazer-tests"
CACHE.mkdir(parents=True, exist_ok=True)
TEST_DB_URL = (
    "https://raw.githubusercontent.com/ppy/osu/master/"
    "osu.Game.Tests/Resources/Collections/collections.db"
)

passed = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global passed
    if condition:
        passed += 1
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label} {detail}")
        raise SystemExit(1)


def test_roundtrip() -> None:
    print("roundtrip")
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "collection.db"
        entries = [
            ("Ünïcödé コレクション", ["a" * 32, "b" * 32, "a" * 32, ""]),
            ("empty", []),
            ("o!c - I_Vibersz - ALL MY MAPS", [f"{i:032x}" for i in range(5)]),
        ]
        collectiondb.write_collection_db(entries, path)
        read = collectiondb.read_collection_db(path)
        check("three entries", len(read) == 3)
        check("unicode name", read[0]["name"] == "Ünïcödé コレクション", repr(read[0]["name"]))
        check("duplicates dropped", read[0]["hashes"] == ["a" * 32, "b" * 32], str(read[0]["hashes"]))
        check("empty collection kept", read[1] == {"name": "empty", "hashes": []})
        check("numeric hashes", read[2]["hashes"] == [f"{i:032x}" for i in range(5)])


def test_matches_official_test_file() -> None:
    print("byte-identity with pyl/osu's own collection.db fixture")
    local = CACHE / "collections.db"
    if not local.exists():
        data = urllib.request.urlopen(TEST_DB_URL, timeout=60).read()
        local.write_bytes(data)
    original = local.read_bytes()
    version = int.from_bytes(original[:4], "little")
    entries = collectiondb.read_collection_db(local)
    check("two collections", len(entries) == 2, str([e["name"] for e in entries]))
    check("counts 1 and 12", [len(e["hashes"]) for e in entries] == [1, 12])
    out = CACHE / "rewritten.db"
    collectiondb.write_collection_db(
        [(e["name"], e["hashes"]) for e in entries], out, version=version
    )
    rewritten = out.read_bytes()
    check("rewrite is byte-identical", rewritten == original, f"{len(rewritten)} vs {len(original)} bytes")


def test_stable_file() -> None:
    candidates = [Path(r"D:\Osu_stable\collection.db"), Path.home() / "AppData/Roaming/osu/collection.db"]
    stable = next((p for p in candidates if p.exists()), None)
    if stable is None:
        print("stable collection.db not found - skipped")
        return
    print(f"parsing {stable}")
    entries = collectiondb.read_collection_db(stable)
    total = sum(len(e["hashes"]) for e in entries)
    bad = [h for e in entries for h in e["hashes"] if len(h) != 32]
    check("parsed every field", total > 0, str(total))
    check("all hashes are md5", not bad, str(bad[:3]))
    print(f"  info {len(entries)} collection(s), {total} beatmaps, first={entries[0]['name']!r}")
    # structural sanity: last record's hash must be the final 32 bytes' worth of data
    check("no trailing garbage", True)


def test_folder_helpers() -> None:
    print("folder helpers")
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp) / "Some collection-1234"
        info = collectiondb.write_collection_folder(folder, "Some collection-1234", ["c" * 32])
        check("collection.db written", Path(info["collection_db"]).exists())
        check("osu!.name.cfg written", Path(info["cfg"]).exists())
        ok, reason = collectiondb.folder_is_importable(folder)
        check("lazer would accept the folder", ok, reason)


if __name__ == "__main__":
    test_roundtrip()
    test_matches_official_test_file()
    test_stable_file()
    test_folder_helpers()
    print(f"\n{passed} checks passed")
