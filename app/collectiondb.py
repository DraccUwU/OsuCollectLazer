"""Legacy osu! `collection.db` writer/reader.

Format (osu!stable / lazer `LegacyCollectionImporter`):
    int32  version           (ignored by lazer on read; stable writes its build date)
    int32  collection count
      string name            (0x0B marker + 7-bit-encoded length + UTF-8, or a single 0x00 for null)
      int32  beatmap count
        string md5-hash (32 lowercase hex chars)
All integers little-endian.

Beatmaps are referenced by per-difficulty MD5, so a collection can be imported
before (or without) the beatmaps themselves.
"""
from __future__ import annotations

import struct
from pathlib import Path

DB_VERSION = 20150203
STRING_MARKER = 0x0B


class CollectionDbError(ValueError):
    pass


def _write_uleb(out: bytearray, value: int) -> None:
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return


def _write_string(out: bytearray, text: str | None) -> None:
    if text is None:
        out.append(0)
        return
    data = text.encode("utf-8")
    out.append(STRING_MARKER)
    _write_uleb(out, len(data))
    out.extend(data)


def write_collection_db(entries, path: str | Path, version: int = DB_VERSION) -> Path:
    """entries: iterable of (name, [md5, ...]) or dicts {'name':..., 'hashes':[...]}.

    Duplicate hashes inside one entry are dropped (matching osu-collect and stable).
    `version` exists so tests can reproduce files written by other tools byte-for-byte.
    """
    normalised: list[tuple[str, list[str]]] = []
    for entry in entries:
        if isinstance(entry, dict):
            name, hashes = entry["name"], entry["hashes"]
        else:
            name, hashes = entry
        hashes = list(dict.fromkeys(h for h in hashes if h))
        normalised.append((name, hashes))

    out = bytearray()
    out.extend(struct.pack("<i", version))
    out.extend(struct.pack("<i", len(normalised)))
    for name, hashes in normalised:
        _write_string(out, name)
        out.extend(struct.pack("<i", len(hashes)))
        for h in hashes:
            _write_string(out, h)

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(bytes(out))
    return p


def _read_uleb(data: bytes, i: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        byte = data[i]
        i += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, i
        shift += 7
        if shift > 28:
            raise CollectionDbError("varint too long")


def _read_string(data: bytes, i: int) -> tuple[str | None, int]:
    marker = data[i]
    i += 1
    if marker == 0:
        return None, i
    if marker != STRING_MARKER:
        raise CollectionDbError(f"unexpected string marker 0x{marker:02x} at offset {i - 1}")
    length, i = _read_uleb(data, i)
    return data[i : i + length].decode("utf-8", "replace"), i + length


def read_collection_db(path: str | Path) -> list[dict]:
    """Parse a collection.db into [{'name':..., 'hashes':[...]}, ...]."""
    data = Path(path).read_bytes()
    if len(data) < 8:
        return []
    count = struct.unpack_from("<i", data, 4)[0]
    if count < 0 or count > 100_000:
        raise CollectionDbError(f"implausible collection count {count}")
    i = 8
    out = []
    for _ in range(count):
        name, i = _read_string(data, i)
        n = struct.unpack_from("<i", data, i)[0]
        i += 4
        hashes = []
        for _ in range(n):
            h, i = _read_string(data, i)
            if h:
                hashes.append(h)
        out.append({"name": name, "hashes": hashes})
    return out


# ---------------------------------------------------------------------------
# Download-folder helpers
# ---------------------------------------------------------------------------

OSU_CFG_NAME = "osu!.name.cfg"


def write_collection_folder(folder: str | Path, collection_name: str, hashes: list[str], extra_entries=None) -> dict:
    """Write `collection.db` + the dummy `osu!.name.cfg` that makes lazer accept
    this folder as a "previous osu! install" in its import screen."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    entries = [{"name": collection_name, "hashes": list(hashes)}]
    for name, entry_hashes in extra_entries or []:
        entries.append({"name": name, "hashes": list(entry_hashes)})
    db_path = write_collection_db(entries, folder / "collection.db")
    cfg_path = folder / OSU_CFG_NAME
    cfg_path.write_text("", encoding="utf-8")
    return {"collection_db": str(db_path), "cfg": str(cfg_path), "collections": [e["name"] for e in entries]}


def folder_is_importable(folder: str | Path) -> tuple[bool, str]:
    """Mirror lazer's own `IsUsableForStableImport` check."""
    folder = Path(folder)
    if not folder.is_dir():
        return False, "folder does not exist"
    if any(folder.glob("osu!.*.cfg")):
        return True, "accepted (osu!.*.cfg present)"
    if (folder / "Songs").is_dir() or (folder / "Skins").is_dir():
        return True, "accepted (Songs/Skins present)"
    return False, "lazer would reject this folder: no osu!.*.cfg and no Songs/Skins directory"
