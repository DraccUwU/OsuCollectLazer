# OsuCollectLazer

Browse **osu!collector.com** collections and get them into **osu!lazer** with one click:
the app downloads the collection from public beatmap mirrors, hands every beatmapset
straight to your running lazer (no drag-and-drop, no import screen), deletes the archives
again as lazer confirms them so nothing sits on disk twice, and writes the collection
entry into lazer's database — so the collection shows up under Collections without a
single in-game step.

```
start.bat          double-click → opens the app at http://127.0.0.1:8765/
```

## Quick start

```bash
git clone https://github.com/DraccUwU/OsuCollectLazer.git
cd OsuCollectLazer

# the one binary the app uses: a small .NET helper that writes collections into lazer's
# database through lazer's own importer (needs the .NET 10 SDK)
dotnet build -c Release tools/LazerDb

# run it
start.bat            # or:  python -m app.server
```

Then paste a collection link (or search osu!collector) and hit **Download** — the maps are
imported and deleted as they arrive, and the collection appears in lazer. Everything is
plain Python standard library: no pip install, no build step for the app itself.

Without the .NET helper the app still works; only the collection entry is left inside the
downloaded folder for lazer's own import screen instead of being written directly.

## Tests

```bash
python tests/test_collectiondb.py   # 14 checks, incl. byte-identity with ppy/osu's fixture
python tests/test_importlog.py      # 12 checks for the import-confirmation watcher
```

Both also run on every push in CI (`.github/workflows/tests.yml`).

## What it does

1. **Find** — search osu!collector from the app, or paste a collection URL/ID.
2. **Download** — pulls every beatmapset in the collection in parallel from a pool of
   public mirrors (nerinyan, beatconnect, catboy.best, osu.direct, sayobot, nekoha,
   osudl, hinamizawa), verifying each archive, resuming interrupted runs and skipping
   files that are already on disk.
3. **Import the maps** — hands the archives to a running osu!lazer by writing import
   messages straight into the game's own IPC pipe (starting the game first if needed).
   No drag-and-drop, no import screen, and no `osu!.exe` launcher process per batch.
4. **Delete them again** — as soon as lazer's log confirms an import, the `.osz` files
   are removed, so a huge collection never has to fit on disk twice. Space is freed
   batch by batch while the import is still running.
5. **Add the collection entry** — written straight into lazer's realm database through
   lazer's own `LegacyCollectionImporter`, so it shows up under Collections with no
   in-game steps (a timestamped `client.realm` backup is taken first, and the write is
   skipped entirely if the installed lazer's schema version doesn't match the helper's).

The whole thing is one click: **Download** (in the search results or a collection page)
runs 2→5 by itself. Pipeline behaviour is in Settings → "after a download finishes":

| mode | what happens |
|---|---|
| `auto` (default) | import maps → confirm → delete them → write the collection. Nothing to do in-game. |
| `wizard` | stage everything for lazer's own import screen (manual; this is the only way to get **one** import task for all maps) |
| `manual` | download only, then use the Library buttons |

### The one thing that costs knobs: import tasks vs. clicks

lazer posts **one progress task per import call** (`RealmArchiveModelImporter.Import`), and
its IPC channel carries exactly one path per message — `osu!.exe a.osz b.osz …` loops and
sends one message per file, so pushing 200 maps shows 200 tasks. Only lazer's import
screen batches a folder into a single task ("Imported X of Y beatmaps"), and that screen
needs clicks. So it's a straight trade: **zero clicks → one task per map**, **one task →
the `wizard` mode**. This app defaults to zero clicks.

Two dead ends worth knowing about (both verified, don't retry them): lazer is built on
SDL3, which takes file drops through OLE `IDropTarget`, **not** `WM_DROPFILES` — posting
a drop at its window does nothing (and Windows refuses cross-process
`PostMessage(WM_DROPFILES)` with `ERROR_INVALID_HANDLE` anyway). And a bare `collection.db`
is accepted silently and ignored by the IPC forward — collections only enter through the
realm, which is what step 5 does.

## Import speed

Measured on this machine (86-map and 89-map collections), from the app's own job log:

| phase | rate |
|---|---|
| hand-off to the game (`ipc.py`, straight pipe writes) | **600–680 maps/s** — 89 maps in 0.14 s |
| lazer's own import, per IPC message | **~1–2 maps/s** (lazer is the slow half) |
| **direct import** (`import_transport: direct`, game closed) | **~5–6 maps/s** on small batches, scales with cores — 12 maps in 2.2 s |
| lazer's import, folder import via the wizard | one task, imported with `Parallel.ForEachAsync` over all maps |

So the launcher was never the bottleneck: a batch of 20 paths costs ~0.34 s of process
start-up, and the old code spent most of its wall time *waiting* for the game. Two things
fix the wall-clock picture:

* **`import_transport: auto`** — the app writes `[int32 length][UTF-8 JSON]` frames
  (`{"Type": "<assembly-qualified ArchiveImportMessage>", "Value": {"Path": …}}`) to
  `\\.\pipe\osu-framework-osu-lazer`, which is the same thing `osu!.exe` does after its
  cold start, minus the cold start. The Type string includes the assembly version, so it
  is read off the installed `osu.Game.dll` (`LazerDb --ipc-type`) instead of hardcoded.
  A one-off note: the game binds **one** pipe instance — a client that connects and
  never sends a complete frame wedges the listener until osu!lazer is restarted, so the
  app never probes (see the `IpcStuck` guard in `app/ipc.py`).
* **`stream_import: true`** — maps are handed over while they are still downloading, so
  lazer's ~1 map/s import runs *during* the transfer instead of after it. A 51-map
  collection: download 27 s, maps flowing to the game from second 3, everything imported,
  deleted and the collection written 34 s after the click.

If you want the game to import maps in parallel (cores, not one at a time), that is the
`wizard` mode: lazer's folder import puts every map in one `Import` call, which is the
only path that runs `Parallel.ForEachAsync`. It costs the in-game clicks described above.

### `import_transport: direct` — the fast one (needs the game closed)

The helper (`tools/LazerDb --beatmaps-from <list> --parallel N`) runs lazer's own
`BeatmapImporter` over the whole batch with `Parallel.ForEachAsync`, writing straight into
lazer's file store and realm — the same code the import screen uses, minus the screen.
Measured ~5–6 maps/s on a 12-map batch and it scales with cores, versus ~1 map/s through
the running game. It also deletes each archive it imports (`ShouldDeleteArchive` for
`.osz`), so it doubles as the "delete after import" step.

Caveats, all handled automatically:

* the game must be **closed** (two writers on one realm); if it is running the job logs
  `direct import needs osu!lazer closed` and uses the IPC pipe for that batch instead,
* it needs `delete_maps_after_import` (the importer removes the archives it takes, so
  "keep the library" can't use it) — that combination falls back to the pipe too,
* if the helper fails for any reason the batch is retried over the pipe, and files whose
  import *failed* are never deleted (they are named in the helper's report).

`LazerDb --check-archive <file.osz>` explains why a particular archive won't import (it
prints what SharpCompress and lazer's own reader see) — useful when a mirror serves
something that unzips in 7-zip but not in lazer.

Downloaded collections land in `Documents\OsuCollectLazer\collections\<name>-<id>\`:

| file | purpose |
|---|---|
| `*.osz` | the beatmapsets — **deleted again** once lazer confirms the import (turn that off in Settings and they are kept instead, with lazer handed hardlinks so its own cleanup can't eat your library) |
| `collection.db` | the collection entry (kept — it's a few KB and documents what the folder was) |
| `osu!.name.cfg` | empty marker that makes lazer accept the folder as a "previous osu! install" |
| `meta.json`, `download-state.json`, `import-state.json` | bookkeeping for resume, plus what the last import/delete did |

## The wizard route (only if you want one import task for everything)

The default pipeline never opens lazer's UI. If you'd rather have a single
"Imported X of Y beatmaps" task (e.g. for a collection of a few hundred maps that you
want to watch), set Settings → *after a download finishes* to `wizard`, then:

1. In lazer: `Ctrl+O` → **General** → **Run setup wizard**.
2. **Next** until the **Import** page.
3. Set **previous osu! install** to the folder the app shows (copy button in the
   *import steps* dialog):
   * `<collection folder>\.lazer-import` when maps are staged → imports **all maps as
     one task** plus the collection entry,
   * the plain collection folder otherwise → imports only the collection entry.
4. **Import content from previous version**.

lazer merges collections by name, so repeating this never duplicates anything. Be aware
that staging unpacks a **second full copy** of the collection while it waits — that is how
a 285 GB collection once needed 570 GB. The `free space` button (or the automatic
cleanup when the import finishes) removes the staged copies again.

## Download speed notes

Downloads run 10-wide by default (Settings → concurrent downloads) across a pool that
probes every mirror at the start of a run and then favours whichever mirrors measured
fastest, demoting ones that error. Observed on a 27-set collection: peak 18.6 MB/s,
median transfer 6.9 MB/s, 27/27 sets with zero failures. Per-run timeline (mirror, bytes,
seconds) is in the job JSON at `/api/jobs`; live mirror stats at `/api/mirrors`.

Handing the maps to lazer is fast too — straight into the game's IPC pipe, 600+ maps/s
(see **Import speed** above); a 4-set collection goes from download to "imported and
deleted" in ~3 seconds once the game is up.

## Deleting downloads

The one-click pipeline already deletes each batch of `.osz` files the moment lazer
confirms it imported them, so normally there is nothing left to clean up. For everything
else, **Library → Delete all downloads** shows a summary first (collections, map archives,
staged copies, total on disk) and then offers:

* **staged copies only** — the unpacked copies lazer's import screen reads, nothing else
  (the per-collection `free space` button does the same for one collection),
* **delete everything** — every collection folder in the download directory: maps,
  staged copies, `collection.db`, bookkeeping.

It refuses to run while a download / stage / push job is still going (409), can be limited
to a single folder, and has a server-side dry-run mode. Beatmaps already inside lazer stay
there, and the app's settings are never touched.

```bash
curl -X POST localhost:8765/api/library/delete -H 'Content-Type: application/json' \
     -d '{"scope":"all","dry_run":true}'      # report only
curl -X POST localhost:8765/api/library/delete -H 'Content-Type: application/json' \
     -d '{"scope":"staged"}'                  # free the unpacked copies
curl -X POST localhost:8765/api/library/delete -H 'Content-Type: application/json' \
     -d '{"scope":"all","folder":"C:/.../collections/warmup-2179"}'   # one collection
```

## Requirements

* Windows, Python 3.10+ (only for the local server; no third-party packages)
* osu!lazer — the app starts it if it isn't running (turn that off in Settings if you
  don't want the game to take over the screen)
* .NET **10** SDK — only to build `tools/LazerDb` (the helper that writes collections into
  lazer's database); without it the app falls back to leaving collections for lazer's
  import screen

## Layout

```
app/
  server.py        local HTTP server + JSON API (127.0.0.1:8765, single instance)
  collector.py     osu!collector client: /api/collections/{id} + /all?search= listing
  mirrors.py       mirror URL templates, rotation, per-mirror cooldowns
  downloader.py    parallel downloader, integrity checks, resume, stall guards
  collectiondb.py  legacy collection.db writer/reader (byte-exact, see tests)
  lazer.py         lazer detection + IPC hand-off (pipe first, launcher fallback)
  ipc.py           direct client for the game's import pipe (no launcher process)
  importlog.py     watches lazer's database log to confirm imports before deleting
  lazerdb.py       writes collections into lazer's realm via tools/LazerDb (backup + verify)
  batch.py         stages a collection for lazer's import screen (wizard mode)
  library.py       download folder scanning / import state
  web/             the UI (vanilla HTML/CSS/JS)
tools/LazerDb/     small .NET console app: LegacyCollectionImporter + parallel BeatmapImporter
tests/test_collectiondb.py   14 checks incl. byte-identity with ppy/osu's own fixture
tests/test_importlog.py      12 checks for the import-confirmation watcher
tests/test_ipc.py            8 checks for the pipe framing + message type
tests/test_lazerdb.py        13 checks for the direct-import plumbing
```

## Notes from building this

* **lazer's IPC import**: a second `osu!.exe <file…>` launch while lazer is running
  forwards the paths to the primary instance (`ArchiveImportIPCChannel` in `osu.Game/IPC`),
  **one message per file** — so a launch with 20 paths still becomes 20 import tasks.
  Handled extensions are `.osz .olz .osk .osr` only — a bare `collection.db` is accepted
  silently and ignored.
* **lazer deletes archives it imports** (`ShouldDeleteArchive`). That's what the pipeline
  relies on for freeing space, and why "keep the maps" mode hands lazer hardlinks instead
  of the originals.
* **Each lazer run writes its own `<id>.database.log`**, so an import watcher must pick
  the marker from the right file — a marker taken from the previous run's log sees zero
  confirmations even though everything imported. `importlog.wait_for_log_after()` handles
  a game the app just launched, and `read_since()` follows to a newer file.
* **lazer's IPC pipe is single-instance** (`new NamedPipeServerStream(name, PipeDirection.InOut, 1)`)
  and accepts one connection at a time. Connecting and closing *without* sending a frame
  leaves the listener unusable — every later send fails with `ERROR_PIPE_BUSY` (231) and
  even `osu!.exe <file>` times out with `IPCTimeoutException`, until the game is
  restarted. Never write a "is it listening?" probe; send a real frame or nothing.
* **Importing maps outside the game needs the ruleset assemblies.** Without them the
  legacy `.osu` decoder cannot instantiate a game mode and *every* map fails to parse —
  which surfaces as `No valid beatmap files found in the beatmap archive`, pointing at
  the archive instead of the missing rulesets (that message is thrown when the parsed
  beatmap list comes out empty, not when the zip is bad). Hence the four
  `ppy.osu.Game.Rulesets.*` package references in `tools/LazerDb`.
* An archive that already exists in lazer counts as imported (lazer's "skip import"
  path) and still gets deleted — that is how a re-run over the same folder works.
* **Deleting is gated on lazer's own log**: files are only removed once the confirmed +
  failed counts cover the batch, so an import that silently didn't happen leaves the
  archives in place.
* Some mirrors redirect to each other (catboy.best ↔ beatconnect), a few rate-limit
  after a burst (per-mirror cooldowns are automatic; a slow mirror is dropped after 30s
  below 25 KB/s so it can't stall a run), and `nzbasic` ships disabled because it
  returned non-archive bodies when probed.
* Proxies/AV can briefly lock a freshly written archive on Windows; the move-into-place
  step retries and falls back to copy, and the file-lock case is never blamed on the mirror.

## If you'd rather not use an app

The free route without this tool: [osu-collect](https://github.com/uwuclxdy/osu-collect)
(TUI) downloads a collection to a folder exactly like this app does, and lazer's
import screen takes the collection from it. The official osu!Collector desktop app
(paid) states that lazer collection integration is *not* supported, so lazer's own
import screen or this app are the only routes.

Also verified along the way, so nobody has to rediscover it: [osu-import](https://github.com/R3dWolfie/osu-import)
feeds files to a running lazer, but it goes through the same one-path-per-message IPC
forward, so it is one task per map just like this app's push path.

## Credits

Built against [ppy/osu](https://github.com/ppy/osu) (the app targets its real import
code paths — `ArchiveImportIPCChannel`, `LegacyCollectionImporter`, `ShouldDeleteArchive`)
and using mirror URL templates established by
[osu-collect](https://github.com/uwuclxdy/osu-collect). Beatmap data comes from
[osu!collector](https://osucollector.com/) and the public mirrors, not from this project.

Unofficial: not affiliated with osu!, ppy Pty Ltd, or osu!collector.

## License

[MIT](LICENSE).
