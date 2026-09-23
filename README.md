# OsuCollectLazer

Browse **osu!collector.com** collections and get them into **osu!lazer** with one click:
the app downloads the collection from public beatmap mirrors, imports every beatmapset
straight into lazer's own files and database with lazer's own importer — in parallel, with
the game closed, no import screen and no in-game steps — deletes each archive the moment
lazer has taken it, and writes the collection entry, so the collection shows up under
Collections on its own.

```
start.bat          double-click → opens the app at http://127.0.0.1:8765/
```

## Quick start

```bash
git clone https://github.com/DraccUwU/OsuCollectLazer.git
cd OsuCollectLazer

# the one binary the app uses: lazer's own importers (BeatmapImporter for maps,
# LegacyCollectionImporter for the collection entry), driven in parallel. Needs the
# .NET 10 SDK.  Windows: tools\build.bat   ·   anywhere: dotnet build -c Release tools/LazerDb
dotnet build -c Release tools/LazerDb

# run it
start.bat            # or:  python -m app.server
```

Then paste a collection link (or search osu!collector) and hit **Download** — the maps are
imported and deleted as they arrive, and the collection appears in lazer. Everything is
plain Python standard library: no pip install, no build step for the app itself.

The helper is required: map imports and collection writes both go through it, and the app
says so in the Library if it is missing (`build the helper with tools\build.bat`).

Expect the app to **close osu!lazer** when it imports (the game must not be writing to its
own database while the helper does) — see [Closing the game](#closing-the-game).

## Tests

```bash
python tests/test_collectiondb.py   # 14 checks, incl. byte-identity with ppy/osu's fixture
python tests/test_lazerdb.py        # 13 checks for the import plumbing
```

Both also run on every push in CI (`.github/workflows/tests.yml`).

## What it does

1. **Find** — search osu!collector from the app, or paste a collection URL/ID.
2. **Download** — pulls every beatmapset in the collection in parallel from a pool of
   public mirrors (nerinyan, beatconnect, catboy.best, osu.direct, sayobot, nekoha,
   osudl, hinamizawa), verifying each archive, resuming interrupted runs and skipping
   files that are already on disk.
3. **Import the maps** — the helper runs lazer's own `BeatmapImporter` over each batch with
   `Parallel.ForEachAsync`, writing straight into lazer's file store and realm. ~5–6 maps/s
   and it scales with cores.
4. **Delete them again** — the importer deletes each archive it takes
   (`ShouldDeleteArchive`), so a huge collection never has to fit on disk twice; the job
   accounts for the freed space batch by batch. An archive whose import *failed* is never
   deleted — it stays for a retry and is named in the job log.
5. **Add the collection entry** — written into lazer's realm through lazer's own
   `LegacyCollectionImporter` (a timestamped `client.realm` backup is taken first, and the
   write is skipped entirely if the installed lazer's schema version doesn't match the
   helper's).

The whole thing is one click: **Download** (in the search results or a collection page)
runs 2→5 by itself. Two settings change the shape of it:

| setting | default | what it does |
|---|---|---|
| *after a download finishes* | `auto` | import → delete → write the collection. `manual` = download only, then press **import now** in the Library |
| *import each wave while the download is still running* | on | the first waves are imported *during* the transfer, so the import is hidden inside the download instead of added to it |

With both at their defaults the app holds **one** import job per collection and the maps
are gone from disk as the last wave lands.

## Import speed

Measured on this machine, from the app's own job log:

| path | rate |
|---|---|
| **direct import** (what the app does: helper + `Parallel.ForEachAsync`) | **~5–6 maps/s** — 12 maps in 2.2 s; scales with cores |
| through the running game (what the app used to do) | ~1–2 maps/s — measured 0.73–1.85 maps/s |
| handing paths to the game's IPC pipe | 600–680 maps/s (never the bottleneck) |

Why the game is the slow half: lazer posts **one progress task per import call**
(`RealmArchiveModelImporter.Import`), and its IPC channel carries exactly one path per
message, so the running game imports strictly one map at a time. Only lazer's import
*screen* batches a folder into a single `Import` call, and that needs clicks — which is
exactly the trade this app refuses to make. Importing outside the game sidesteps it: the
helper calls the same importer over the whole batch at once.

A 51-map collection end to end: download 27 s, maps imported in waves from second 8,
everything imported, deleted and the collection written 34 s after the click.

### Closing the game

Two writers on one realm is a corrupt realm, so imports need lazer **closed**. The app
closes it for you (`close osu!lazer automatically` in Settings, on by default):

* first a graceful close — the same request as clicking the X — for up to ~12 s;
* a busy game (mid library scan, mid import, a beatmap running) can ignore that for
  minutes, so after that it is terminated outright. The job log says so:
  `osu!lazer ignored a normal close request (it was busy), so it was terminated —
  anything unsaved in the game is gone`.

Turn the setting off and imports instead refuse to start while the game is running (the
job stops with `close osu!lazer automatically is turned off`, and the archives stay on
disk) — nothing is ever deleted on a failed import.

`LazerDb --check-archive <file.osz>` explains why a particular archive won't import (it
prints what SharpCompress and lazer's own reader see) — useful when a mirror serves
something that unzips in 7-zip but not in lazer.

Downloaded collections land in `Documents\OsuCollectLazer\collections\<name>-<id>\`:

| file | purpose |
|---|---|
| `*.osz` | the beatmapsets — **deleted again** as the importer takes them; when the import is done the folder no longer holds the map files |
| `collection.db` | the collection entry (kept — it's a few KB and documents what the folder was) |
| `osu!.name.cfg` | empty marker that makes lazer accept the folder as a "previous osu! install" |
| `meta.json`, `download-state.json`, `import-state.json` | bookkeeping for resume, plus what the last import did |

## Download speed notes

Downloads run 10-wide by default (Settings → concurrent downloads) across a pool that
probes every mirror at the start of a run and then favours whichever mirrors measured
fastest, demoting ones that error. Observed on a 27-set collection: peak 18.6 MB/s,
median transfer 6.9 MB/s, 27/27 sets with zero failures. Per-run timeline (mirror, bytes,
seconds) is in the job JSON at `/api/jobs`; live mirror stats at `/api/mirrors`.

The download is usually the slower half now: a 4-set collection goes from click to
"imported and deleted" in ~3 seconds, and for big collections the import runs inside the
download rather than after it.

## Deleting downloads

The pipeline deletes each archive the moment it is imported, so normally there is nothing
left to clean up. For everything else, **Library → Delete all downloads** shows a summary
first (collections, total on disk) and then deletes every collection folder in the
download directory — map archives, `collection.db`, bookkeeping. Beatmaps already inside
lazer stay there, and the app's settings are never touched.

It refuses to run while a download or import job is still going (409), can be limited to a
single folder, and has a server-side dry-run mode.

```bash
curl -X POST localhost:8765/api/library/delete -H 'Content-Type: application/json' \
     -d '{"scope":"all","dry_run":true}'      # report only
curl -X POST localhost:8765/api/library/delete -H 'Content-Type: application/json' \
     -d '{"scope":"all"}'                     # delete everything
curl -X POST localhost:8765/api/library/delete -H 'Content-Type: application/json' \
     -d '{"scope":"all","folder":"C:/.../collections/warmup-2179"}'   # one collection
```

## Requirements

* Windows, Python 3.10+ (only for the local server; no third-party packages)
* osu!lazer — the app closes it when it needs to import, and never starts it
* .NET **10** SDK — to build `tools/LazerDb`, the helper that does the importing

## Layout

```
app/
  server.py        local HTTP server + JSON API (127.0.0.1:8765, single instance)
  collector.py     osu!collector client: /api/collections/{id} + /all?search= listing
  mirrors.py       mirror URL templates, rotation, per-mirror cooldowns
  downloader.py    parallel downloader, integrity checks, resume, stall guards
  collectiondb.py  legacy collection.db writer/reader (byte-exact, see tests)
  lazer.py         lazer detection, data-directory lookup, closing the game for imports
  lazerdb.py       drives tools/LazerDb: parallel map import + realm collection write
  library.py       download folder scanning / import state
  web/             the UI (vanilla HTML/CSS/JS)
tools/LazerDb/     small .NET console app: parallel BeatmapImporter + LegacyCollectionImporter
tools/build.bat    build it (dotnet build -c Release)
tests/test_collectiondb.py   14 checks incl. byte-identity with ppy/osu's own fixture
tests/test_lazerdb.py        13 checks for the import plumbing
```

## Notes from building this

* **Importing maps outside the game needs the ruleset assemblies.** Without them the
  legacy `.osu` decoder cannot instantiate a game mode and *every* map fails to parse —
  which surfaces as `No valid beatmap files found in the beatmap archive`, pointing at
  the archive instead of the missing rulesets (that message is thrown when the parsed
  beatmap list comes out empty, not when the zip is bad). Hence the four
  `ppy.osu.Game.Rulesets.*` package references in `tools/LazerDb`.
* **lazer deletes archives it imports** (`ShouldDeleteArchive`), which is what frees the
  space as the import runs.
* An archive that already exists in lazer counts as imported (lazer's "skip import" path)
  and still gets deleted — that is how a re-run over the same folder works, and it is
  idempotent: the realm's beatmapset count only moves for maps that were genuinely new.
* **Deletion is gated on the helper's report**, per file: anything in `failed_files` stays
  on disk, and a job that fails before importing deletes nothing at all.
* **A busy osu!lazer ignores a graceful close.** `taskkill` without `/F` reports
  `SUCCESS: Sent termination signal` while the game keeps running through a long library
  scan (and a bare `WM_CLOSE` posted at its SDL window does nothing either), so the app
  escalates to `/F` after ~12 s instead of pretending the close worked.
* lazer's own IPC import is one path per message (`ArchiveImportIPCChannel`), which is why
  going *through* the running game means one import task and ~1 s per map. Handled
  extensions are `.osz .olz .osk .osr` only — a bare `collection.db` is accepted silently
  and ignored, collections only enter through the realm.
* File drops at the game don't work as an alternative: lazer is built on SDL3, which takes
  drops through OLE `IDropTarget`, **not** `WM_DROPFILES` — posting a drop at its window
  does nothing (and Windows refuses cross-process `PostMessage(WM_DROPFILES)` with
  `ERROR_INVALID_HANDLE` anyway).
* Some mirrors redirect to each other (catboy.best ↔ beatconnect), a few rate-limit
  after a burst (per-mirror cooldowns are automatic; a slow mirror is dropped after 30s
  below 25 KB/s so it can't stall a run), and `nzbasic` ships disabled because it
  returned non-archive bodies when probed.
* Proxies/AV can briefly lock a freshly written archive on Windows; the move-into-place
  step retries and falls back to copy, and the file-lock case is never blamed on the mirror.
* lazer's realm schema version is checked at runtime against the installed game
  (`RealmAccess.schema_version`): the helper refuses to write if they differ, rather than
  risking a database the game can't open.

## If you'd rather not use an app

The free route without this tool: [osu-collect](https://github.com/uwuclxdy/osu-collect)
(TUI) downloads a collection to a folder exactly like this app does, and lazer's
import screen takes the collection from it. The official osu!Collector desktop app
(paid) states that lazer collection integration is *not* supported, so lazer's own
import screen or this app are the only routes.

Also verified along the way, so nobody has to rediscover it: [osu-import](https://github.com/R3dWolfie/osu-import)
feeds files to a running lazer, but it goes through the same one-path-per-message IPC
forward, so it is one task per map and ~1 s per map.

## Credits

Built against [ppy/osu](https://github.com/ppy/osu) (the helper drives its real import
code paths — `BeatmapImporter`, `LegacyCollectionImporter`, `ShouldDeleteArchive`) and
using mirror URL templates established by
[osu-collect](https://github.com/uwuclxdy/osu-collect). Beatmap data comes from
[osu!collector](https://osucollector.com/) and the public mirrors, not from this project.

Unofficial: not affiliated with osu!, ppy Pty Ltd, or osu!collector.

## License

[MIT](LICENSE).
