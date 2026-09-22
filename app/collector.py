"""osu!collector.com client: collection fetch, search, URL parsing.

Only public, unauthenticated endpoints are used.
  * API:      GET https://osucollector.com/api/collections/{id}
  * Search:   GET https://osucollector.com/all?search=<query>&page=<n>   (server-rendered HTML)
"""
from __future__ import annotations

import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://osucollector.com"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class CollectorError(RuntimeError):
    pass


def _get(url: str, timeout: int = 30, retries: int = 3) -> str:
    last: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", "replace")[:200]
            except Exception:
                pass
            if exc.code == 404:
                raise CollectorError(f"not found (404){': ' + body if body else ''}")
            last = exc
        except Exception as exc:  # network hiccup, retry
            last = exc
        time.sleep(1.0 + attempt)
    raise CollectorError(f"request failed: {last}")


def parse_ref(text: str) -> int | None:
    """Accept a collection ID, an osucollector URL, or a blob containing one."""
    if not text:
        return None
    text = text.strip()
    if text.isdigit():
        return int(text)
    m = re.search(r"/collections/(\d+)", text)
    if m:
        return int(m.group(1))
    m = re.search(r"\b(\d{2,8})\b", text)
    return int(m.group(1)) if m else None


def fetch_collection(cid: int) -> dict:
    raw = _get(f"{BASE}/api/collections/{cid}")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CollectorError(f"bad API response: {exc}") from exc


def summarize(c: dict) -> dict:
    """Derived stats used by the UI and by import verification."""
    sets = c.get("beatmapsets") or []
    set_ids: list[int] = []
    checksums: list[str] = []
    for bs in sets:
        bid = bs.get("id")
        if isinstance(bid, int) and bid not in set_ids:
            set_ids.append(bid)
        for bm in bs.get("beatmaps") or []:
            ck = bm.get("checksum")
            if ck:
                checksums.append(ck)
    uniq = list(dict.fromkeys(checksums))
    return {
        "id": c.get("id"),
        "name": c.get("name") or "",
        "description": c.get("description") or "",
        "uploader": (c.get("uploader") or {}).get("username") or "",
        "uploader_id": (c.get("uploader") or {}).get("id"),
        "favourites": c.get("favourites", 0),
        "beatmap_count": c.get("beatmapCount", len(checksums)),
        "set_count": len(set_ids),
        "checksum_count": len(uniq),
        "unsubmitted": c.get("unsubmittedBeatmapCount", 0),
        "unknown": c.get("unknownChecksums", 0),
        "modes": c.get("modes") or {},
        "date_uploaded": c.get("dateUploaded"),
        "date_modified": c.get("dateLastModified"),
        "set_ids": set_ids,
        "checksums": uniq,
    }


_TAG_RE = re.compile(r"<[^>]+>")
_CARD_SPLIT = re.compile(r'<div class="flex h-full flex-col overflow-hidden rounded-lg')


def _text_of(fragment: str) -> str:
    text = _TAG_RE.sub(" ", fragment)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def search(query: str, page: int = 1, sort_by: str | None = None, order: str = "desc") -> dict:
    """Search collections via the site's listing page."""
    params = {"search": query, "page": page}
    if sort_by:
        params["sortBy"] = sort_by
        params["orderBy"] = order
    url = f"{BASE}/all?{urllib.parse.urlencode(params)}"
    body = _get(url, retries=2)

    results = []
    seen = set()
    for block in _CARD_SPLIT.split(body)[1:]:
        m = re.search(r'href="/collections/(\d+)/([^"]*)"', block)
        if not m:
            continue
        cid = int(m.group(1))
        if cid in seen:
            continue
        seen.add(cid)
        slug = urllib.parse.unquote(m.group(2))
        name = slug.replace("-", " ").strip() or f"collection {cid}"
        # everything after the chart <a> is the card's text (name, description, counts)
        tail = block.split("</a>", 1)[1] if "</a>" in block else block
        snippet = re.sub(r"^[\s\d]+", "", _text_of(tail))[:180]
        fav = re.search(r"(\d[\d,]*)\s*(?:★|favourite|fav)", tail, re.I)
        results.append(
            {
                "id": cid,
                "name": name,
                "url": f"{BASE}/collections/{cid}",
                "snippet": snippet,
                "favourites": int(fav.group(1).replace(",", "")) if fav else None,
            }
        )

    pages = {int(p) for p in re.findall(r"/all\?page=(\d+)", body)}
    has_next = any(p > page for p in pages) or len(results) >= 48
    return {"query": query, "page": page, "url": url, "results": results, "has_next": has_next}
