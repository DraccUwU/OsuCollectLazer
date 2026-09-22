"""Beatmap mirror pool: URL templates, latency probing, speed-weighted rotation.

Templates mirror the ones osu-collect ships (verified live for the enabled set).
osu! official is deliberately excluded (needs account auth and is heavily rate
limited).

Selection policy:
  * mirrors that have not served a transfer yet are tried first (so the pool learns),
  * then the mirror with the best measured throughput,
  * a mirror that errors sits out its cooldown, but a 404 (this mirror simply does
    not carry that beatmapset) does NOT cost the mirror its turn.
"""
from __future__ import annotations

import random
import threading
import time
import urllib.error
import urllib.request

MIRRORS: dict[str, dict] = {
    "nerinyan": {
        "video": "https://api.nerinyan.moe/d/{id}",
        "novideo": "https://api.nerinyan.moe/d/{id}?nv=1",
        "cooldown": 45,
    },
    "beatconnect": {
        "video": "https://beatconnect.io/b/{id}/",
        "novideo": "https://beatconnect.io/b/{id}/?novideo=1",
        "cooldown": 45,
    },
    "catboy": {
        "video": "https://catboy.best/d/{id}",
        "novideo": "https://catboy.best/d/{id}n",
        "cooldown": 45,
    },
    "osu.direct": {
        "video": "https://osu.direct/d/{id}",
        # the old `/d/{id}n` suffix 404s for every set now; the query form works
        "novideo": "https://osu.direct/d/{id}?nv=1",
        "cooldown": 75,
    },
    "sayobot": {
        "video": "https://dl.sayobot.cn/beatmaps/download/full/{id}",
        "novideo": "https://dl.sayobot.cn/beatmaps/download/novideo/{id}",
        "cooldown": 45,
    },
    "nekoha": {
        "video": "https://mirror.nekoha.moe/api/download/{id}",
        "novideo": "https://mirror.nekoha.moe/api/download/{id}",
        "cooldown": 45,
    },
    "osudl": {
        "video": "https://osudl.org/s/{id}",
        "novideo": "https://osudl.org/s/{id}?video=false",
        "cooldown": 45,
        "ranked_only": True,
    },
    "hinamizawa": {
        "video": "https://mirror.hinamizawa.ai/api/v1/hinai/d/{id}",
        "novideo": "https://mirror.hinamizawa.ai/api/v1/hinai/d/{id}?no_video=true",
        "cooldown": 45,
    },
    # flaky in practice (served a non-zip body when probed); off by default
    "nzbasic": {
        "video": "https://direct.nzbasic.com/{id}.osz",
        "novideo": "https://direct.nzbasic.com/{id}.osz",
        "cooldown": 90,
    },
}

DEFAULT_ENABLED = [
    "nerinyan",
    "beatconnect",
    "catboy",
    "osu.direct",
    "sayobot",
    "nekoha",
    "osudl",
    "hinamizawa",
]

PROBE_UA = "OsuCollectLazer/1.0 (+local)"


class MirrorPool:
    """Speed-weighted mirror selection with per-mirror cooldowns."""

    def __init__(self, enabled: list[str] | None = None, spacing: float = 0.05):
        names = [n for n in (enabled or DEFAULT_ENABLED) if n in MIRRORS]
        if not names:
            raise ValueError("no usable mirrors enabled")
        self.names = names
        self.spacing = spacing
        self._lock = threading.Lock()
        self._next_allowed = {n: 0.0 for n in names}
        self._last_used = {n: 0.0 for n in names}
        self._ema = {n: 0.0 for n in names}  # bytes/s, 0 while unknown
        self._samples = {n: 0 for n in names}  # times the mirror actually answered
        self._hard_fails = {n: 0 for n in names}  # errors (not 404s)
        self._nf_streak = {n: 0 for n in names}  # consecutive 404s
        self.notfound_cooldown = 20.0
        self.stats = {n: {"ok": 0, "fail": 0, "cooldowns": 0, "not_found": 0} for n in names}
        self.probe_ms: dict[str, float] = {}

    # -- URL helpers --------------------------------------------------------
    def url_for(self, name: str, set_id: int, no_video: bool) -> str:
        key = "novideo" if no_video else "video"
        return MIRRORS[name][key].format(id=set_id)

    # -- selection ----------------------------------------------------------
    def _score(self, name: str, now: float) -> tuple:
        # A mirror counts as "learned" once it has answered at all — including 404s
        # (answering "I don't have it" is information). A mirror that only ever
        # errored is learned too, so it stops being tried first.
        proven_bad = self._hard_fails[name] >= 2 and self._samples[name] == 0
        learned = self._samples[name] > 0 or proven_bad
        return (
            0 if learned else 1,
            self._ema[name],
            now - self._last_used[name],
            random.random(),
        )

    def acquire(self, timeout: float = 300.0) -> str:
        """Return a mirror that is allowed to serve a request right now."""
        deadline = time.monotonic() + timeout
        while True:
            now = time.monotonic()
            with self._lock:
                available = [n for n in self.names if self._next_allowed[n] <= now]
                if available:
                    name = max(available, key=lambda n: self._score(n, now))
                    wait = max(0.0, self._last_used[name] + self.spacing - now)
                    self._next_allowed[name] = now + wait
                    self._last_used[name] = now + wait
                    return name
                soonest = min(self._next_allowed.values())
            if now >= deadline:
                raise TimeoutError("all mirrors are cooling down")
            time.sleep(min(0.5, max(0.02, soonest - now)))

    def report(
        self,
        name: str,
        ok: bool,
        *,
        cooldown: bool = True,
        speed: float | None = None,
        not_found: bool = False,
    ) -> None:
        if name not in self.stats:
            return
        with self._lock:
            if not_found:
                # this mirror answered, it just does not carry that beatmapset:
                # no cooldown, but it stops being treated as unproven, and a mirror
                # 404ing everything repeatedly gets a short breather instead of
                # being hammered on every set.
                self.stats[name]["not_found"] += 1
                self._samples[name] += 1
                self._nf_streak[name] += 1
                if self._nf_streak[name] >= 3:
                    self._nf_streak[name] = 0
                    self._next_allowed[name] = time.monotonic() + self.notfound_cooldown
                return
            if ok:
                self.stats[name]["ok"] += 1
                self._nf_streak[name] = 0
                if speed:
                    self._ema[name] = speed if self._samples[name] == 0 else 0.65 * self._ema[name] + 0.35 * speed
                self._samples[name] += 1
            else:
                self.stats[name]["fail"] += 1
                self._hard_fails[name] += 1
                if cooldown:
                    self.stats[name]["cooldowns"] += 1
                    self._next_allowed[name] = time.monotonic() + MIRRORS[name]["cooldown"]

    # -- latency probe ------------------------------------------------------
    def probe(self, sample_set_id: int = 3756, no_video: bool = True, timeout: float = 8.0) -> dict[str, float]:
        """Measure time-to-first-byte per mirror and reorder fastest-first."""
        results: dict[str, float] = {}

        def run(name: str) -> None:
            url = self.url_for(name, sample_set_id, no_video)
            req = urllib.request.Request(
                url,
                headers={"User-Agent": PROBE_UA, "Range": "bytes=0-0", "Accept": "*/*"},
            )
            started = time.monotonic()
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    resp.read(64)
                    if resp.status in (200, 206):
                        results[name] = (time.monotonic() - started) * 1000
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    results[name] = float("inf")  # reachable, just no such set
            except Exception:
                pass

        threads = [threading.Thread(target=run, args=(n,), daemon=True) for n in self.names]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout + 4)

        with self._lock:
            self.probe_ms = dict(results)
            reachable = [n for n in self.names if results.get(n, float("inf")) != float("inf")]
            unreachable = [n for n in self.names if n not in reachable]
            reachable.sort(key=lambda n: results.get(n, float("inf")))
            self.names = reachable + unreachable
        return dict(results)

    # -- introspection ------------------------------------------------------
    def cooling(self) -> dict[str, float]:
        now = time.monotonic()
        with self._lock:
            return {n: round(max(0.0, t - now), 1) for n, t in self._next_allowed.items() if t > now}

    def snapshot(self) -> list[dict]:
        with self._lock:
            out = []
            for n in self.names:
                out.append(
                    {
                        "name": n,
                        "enabled": True,
                        "cooldown": MIRRORS[n]["cooldown"],
                        "probe_ms": round(self.probe_ms.get(n, -1), 0),
                        "kb_per_s": round(self._ema[n] / 1024, 0) if self._samples[n] else None,
                        "transfers": self._samples[n],
                        **self.stats[n],
                    }
                )
            return out
