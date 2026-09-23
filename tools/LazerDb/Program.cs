// LazerDb — headless helper for OsuCollectLazer.
//
// Writes collections straight into osu!lazer's realm database using lazer's own
// importer (LegacyCollectionImporter) — the same code the in-game "Import content
// from previous version" screen runs. That is the only way to add a collection
// without the user clicking through that screen, so this tool is the "no steps in
// the game" half of the app.
//
// Safety model:
//   * the realm schema version is read from the *installed* osu.Game.dll and compared
//     with the version this binary was built against; on mismatch nothing is opened
//     (a schema version difference could otherwise upgrade the database beyond what
//     the installed game understands),
//   * --dry-run opens the database, reports what would change, writes nothing,
//   * callers (the Python app) take a backup before a real write and verify after.
//
// Usage:
//   LazerDb --versions-for <path to installed osu.Game.dll>
//   LazerDb --ipc-type <path to installed osu.Game.dll>
//   LazerDb --data-dir <lazer data dir> [--collection <collection.db>] [--dry-run]
//   LazerDb --data-dir <lazer data dir> --beatmaps-from <list file> [--parallel N]
//           (imports .osz archives in parallel with lazer's own BeatmapImporter; the
//            list file holds one absolute path per line, '#' comments allowed)

using System.Collections.Concurrent;
using System.Diagnostics;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text.Json;
using osu.Framework.Platform;
using osu.Game.Beatmaps;
using osu.Game.Beatmaps.Formats;
using osu.Game.Collections;
using osu.Game.Database;
using osu.Game.Rulesets;
using osu.Game.Utils;

namespace LazerDb;

internal static class Program
{
    private const BindingFlags const_flags = BindingFlags.NonPublic | BindingFlags.Static;

    private static readonly JsonSerializerOptions json_options = new() { WriteIndented = true };

    private static async Task<int> Main(string[] args)
    {
        string? dataDir = null;
        string? collectionPath = null;
        string? versionsFor = null;
        string? ipcTypeFor = null;
        string? beatmapsFrom = null;
        int? parallel = null;
        string? checkArchive = null;
        bool dryRun = false;

        for (int i = 0; i < args.Length; i++)
        {
            switch (args[i])
            {
                case "--data-dir":
                    dataDir = Next(args, ref i);
                    break;
                case "--collection":
                    collectionPath = Next(args, ref i);
                    break;
                case "--beatmaps-from":
                    beatmapsFrom = Next(args, ref i);
                    break;
                case "--parallel":
                    parallel = int.TryParse(Next(args, ref i), out int p) && p > 0 ? p : null;
                    break;
                case "--versions-for":
                    versionsFor = Next(args, ref i);
                    break;
                case "--ipc-type":
                    ipcTypeFor = Next(args, ref i);
                    break;
                case "--check-archive":
                    checkArchive = Next(args, ref i);
                    break;
                case "--dry-run":
                    dryRun = true;
                    break;
                default:
                    Console.Error.WriteLine($"unknown argument: {args[i]}");
                    return 2;
            }
        }

        try
        {
            string? ownVersion = SchemaVersionOf(typeof(RealmAccess).Assembly);

            if (ipcTypeFor != null)
            {
                string? typeName = IpcTypeOfFile(ipcTypeFor);
                Console.WriteLine(JsonSerializer.Serialize(new
                {
                    ipc_type = typeName,
                    osu_game_dll = ipcTypeFor,
                }, json_options));
                return typeName == null ? 1 : 0;
            }

            if (checkArchive != null)
                return CheckArchive(checkArchive);

            if (versionsFor != null)
            {
                string? installed = SchemaVersionOfFile(versionsFor);
                Console.WriteLine(JsonSerializer.Serialize(new
                {
                    tool_schema_version = ownVersion,
                    installed_schema_version = installed,
                    match = ownVersion == installed,
                    osu_game_dll = versionsFor,
                }, json_options));
                return ownVersion == installed ? 0 : 1;
            }

            if (dataDir == null)
            {
                Console.Error.WriteLine("--data-dir is required (or use --versions-for)");
                return 2;
            }

            string realmPath = Path.Combine(dataDir, "client.realm");
            if (!File.Exists(realmPath))
            {
                Console.Error.WriteLine($"no client.realm in {dataDir}");
                return 3;
            }

            string? installedVersion = installedVersionFor(dataDir);
            if (installedVersion != null && ownVersion != null && installedVersion != ownVersion)
            {
                Console.Error.WriteLine(
                    $"refusing to open: the installed osu!lazer uses realm schema {installedVersion}, this tool was built for {ownVersion}. " +
                    "Rebuild the tool against the matching ppy.osu.Game version.");
                return 4;
            }

            var storage = new NativeStorage(dataDir);

            if (beatmapsFrom != null)
                return await ImportBeatmapsAsync(dataDir, storage, beatmapsFrom, parallel, dryRun).ConfigureAwait(false);

            using var realmAccess = new RealmAccess(storage, "client");

            List<(string name, int hashes)> before = Snapshot(realmAccess);

            var result = new Dictionary<string, object?>
            {
                ["schema_version"] = ownVersion,
                ["installed_schema_version"] = installedVersion,
                ["realm"] = realmPath,
                ["dry_run"] = dryRun,
                ["collections_before"] = before.Select(c => new { name = c.name, hashes = c.hashes }).ToList(),
            };

            if (collectionPath != null && !dryRun)
            {
                using var stream = File.OpenRead(collectionPath);
                // lazer's own importer: merges by name, de-duplicates hashes, bumps LastModified
                new LegacyCollectionImporter(realmAccess).Import(stream).GetAwaiter().GetResult();
                result["imported_from"] = collectionPath;
            }

            List<(string name, int hashes)> after = Snapshot(realmAccess);
            result["collections_after"] = after.Select(c => new { name = c.name, hashes = c.hashes }).ToList();

            if (collectionPath != null)
            {
                var namesBefore = before.Select(c => c.name).ToHashSet(StringComparer.OrdinalIgnoreCase);
                result["added"] = after.Where(c => !namesBefore.Contains(c.name)).Select(c => c.name).ToList();
            }

            Console.WriteLine(JsonSerializer.Serialize(result, json_options));
            return 0;
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine(JsonSerializer.Serialize(new
            {
                error = ex.GetType().Name,
                message = ex.Message,
                inner = ex.InnerException?.Message,
            }, json_options));
            return 5;
        }
    }

    private static string? Next(string[] args, ref int i) => ++i < args.Length ? args[i] : null;

    private static List<(string name, int hashes)> Snapshot(RealmAccess realm) =>
        realm.Run(r => r.All<BeatmapCollection>()
                        .ToList()
                        .Select(c => (name: c.Name, hashes: c.BeatmapMD5Hashes.Count))
                        .OrderBy(c => c.name, StringComparer.OrdinalIgnoreCase)
                        .ToList());

    /// <summary>
    /// Import .osz/.olz archives the way the game's import screen does, but in parallel and
    /// with no running game: the same <see cref="BeatmapImporter"/>, the same file store and
    /// realm, so the result is indistinguishable from an import osu!lazer did itself (minus
    /// the online metadata refresh, which the game does lazily on its own).
    /// </summary>
    private static async Task<int> ImportBeatmapsAsync(string dataDir, NativeStorage storage, string listFile, int? parallel, bool dryRun)
    {
        string[] paths = File.ReadAllLines(listFile)
                             .Select(l => l.Trim())
                             .Where(l => l.Length > 0 && !l.StartsWith('#'))
                             .ToArray();

        string[] missing = paths.Where(p => !File.Exists(p)).ToArray();
        if (missing.Length > 0)
        {
            Console.Error.WriteLine($"missing {missing.Length} archive(s), first: {missing[0]}");
            return 3;
        }

        var report = new Dictionary<string, object?>
        {
            ["beatmaps_requested"] = paths.Length,
            ["dry_run"] = dryRun,
        };

        if (dryRun || paths.Length == 0)
        {
            report["beatmaps_imported"] = 0;
            report["beatmaps_failed"] = 0;
            report["seconds"] = 0.0;
            Console.WriteLine(JsonSerializer.Serialize(report, json_options));
            return 0;
        }

        using var realmAccess = new RealmAccess(storage, "client");

        // what the game does before it imports anything: beatmap parsing needs the ruleset
        // store (the decoder resolves the beatmap's ruleset through it), and touching
        // Decoder at all is what registers the legacy .osu decoder
        using var rulesets = new RealmRulesetStore(realmAccess, storage);
        Decoder.RegisterDependencies(rulesets);

        var importer = new BeatmapImporter(storage, realmAccess);

        int setsBefore = realmAccess.Run(r => r.All<BeatmapSetInfo>().Count());

        int imported = 0, failed = 0, processed = 0;
        var errors = new ConcurrentBag<string>();
        var failedPaths = new ConcurrentBag<string>();
        var stopwatch = Stopwatch.StartNew();

        await Parallel.ForEachAsync(
            paths,
            new ParallelOptions { MaxDegreeOfParallelism = parallel ?? Environment.ProcessorCount },
            async (path, ct) =>
            {
                string name = Path.GetFileName(path);

                try
                {
                    var live = await importer.Import(
                        new ImportTask(path),
                        new ImportParameters { Batch = true, ImportImmediately = true },
                        ct).ConfigureAwait(false);

                    if (live != null)
                        Interlocked.Increment(ref imported);
                    else
                    {
                        Interlocked.Increment(ref failed);
                        failedPaths.Add(path);
                        errors.Add($"{name}: importer returned no model");
                    }
                }
                catch (Exception ex)
                {
                    Interlocked.Increment(ref failed);
                    failedPaths.Add(path);
                    errors.Add($"{name}: {ex.GetType().Name}: {ex.Message}");
                }

                int done = Interlocked.Increment(ref processed);
                if (done % 25 == 0 || done == paths.Length)
                {
                    double rate = stopwatch.Elapsed.TotalSeconds > 0 ? done / stopwatch.Elapsed.TotalSeconds : 0;
                    Console.Error.WriteLine($"progress {done}/{paths.Length} ({rate:F1} maps/s, {imported} ok, {failed} failed)");
                }
            }).ConfigureAwait(false);

        stopwatch.Stop();
        report["beatmaps_imported"] = imported;
        report["beatmaps_failed"] = failed;
        report["seconds"] = Math.Round(stopwatch.Elapsed.TotalSeconds, 2);
        report["errors"] = errors.Take(10).ToList();
        report["failed_files"] = failedPaths.ToList();
        report["beatmap_sets_before"] = setsBefore;
        report["beatmap_sets_after"] = realmAccess.Run(r => r.All<BeatmapSetInfo>().Count());
        Console.WriteLine(JsonSerializer.Serialize(report, json_options));
        return failed == 0 ? 0 : 5;
    }

    /// <summary>
    /// Explain why a .osz may not be importable: lazer's own gate is
    /// <see cref="ZipUtils.IsZipArchive"/>, which decompresses every entry with
    /// SharpCompress and gives up on the first failure — an archive that Python/7-zip
    /// open happily can still fail there, and then the importer falls back to a
    /// single-file reader and reports "No valid beatmap files found".
    /// </summary>
    private static int CheckArchive(string path)
    {
        var result = new Dictionary<string, object?>
        {
            ["path"] = path,
            ["exists"] = File.Exists(path),
        };

        if (!File.Exists(path))
        {
            Console.WriteLine(JsonSerializer.Serialize(result, json_options));
            return 1;
        }

        result["size"] = new FileInfo(path).Length;

        try
        {
            using var arc = SharpCompress.Archives.Zip.ZipArchive.OpenArchive(path);
            var entries = arc.Entries.ToList();
            result["entries"] = entries.Count;
            result["dot_osu_entries"] = entries.Count(e => e.Key != null && e.Key.EndsWith(".osu", StringComparison.OrdinalIgnoreCase));

            var failures = new List<string>();
            var buffer = new byte[81920];
            foreach (var entry in entries)
            {
                try
                {
                    using var stream = entry.OpenEntryStream();
                    while (stream.Read(buffer, 0, buffer.Length) > 0)
                    {
                    }
                }
                catch (Exception ex)
                {
                    failures.Add($"{entry.Key}: {ex.GetType().Name}: {ex.Message}");
                    if (failures.Count >= 5)
                        break;
                }
            }

            result["entry_failures"] = failures;
        }
        catch (Exception ex)
        {
            result["open_error"] = $"{ex.GetType().Name}: {ex.Message}";
        }

        result["lazer_accepts_it"] = ZipUtils.IsZipArchive(path);

        // what the importer's own reader sees (this is the part that decides
        // "No valid beatmap files found in the beatmap archive")
        try
        {
            var task = new ImportTask(path);
            using var reader = task.GetReader();
            var names = reader.Filenames.ToList();
            result["reader_type"] = reader.GetType().Name;
            result["reader_filenames"] = names.Count;
            result["reader_dot_osu"] = names.Count(n => n.EndsWith(".osu", StringComparison.OrdinalIgnoreCase));
            result["reader_first"] = names.Take(3).ToList();
        }
        catch (Exception ex)
        {
            result["reader_error"] = $"{ex.GetType().Name}: {ex.Message}";
        }

        Console.WriteLine(JsonSerializer.Serialize(result, json_options));
        return 0;
    }

    private static string? installedVersionFor(string dataDir)
    {
        string? local = Environment.GetEnvironmentVariable("LOCALAPPDATA");
        if (local == null)
            return null;

        foreach (string candidate in new[]
                 {
                     Path.Combine(local, "osulazer", "current", "osu.Game.dll"),
                     Path.Combine(local, "osulazer", "osu.Game.dll"),
                     Path.Combine(local, "osu", "osu.Game.dll"),
                 })
        {
            if (File.Exists(candidate))
                return SchemaVersionOfFile(candidate);
        }

        return null;
    }

    private static string? SchemaVersionOf(Assembly assembly) =>
        assembly.GetType("osu.Game.Database.RealmAccess")
                ?.GetField("schema_version", const_flags)
                ?.GetRawConstantValue()?.ToString();

    private static string? SchemaVersionOfFile(string osuGameDll)
    {
        try
        {
            var (context, assembly) = LoadInstalled(osuGameDll);
            using (context)
            {
                return assembly.GetType("osu.Game.Database.RealmAccess")
                               ?.GetField("schema_version", const_flags)
                               ?.GetRawConstantValue()?.ToString();
            }
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine($"could not read schema version from {osuGameDll}: {ex.Message}");
            return null;
        }
    }

    /// <summary>
    /// Assembly-qualified name of the IPC message an external client must send to make
    /// the running game import a file (osu.Framework's IpcChannel compares this string
    /// exactly, version included, so it has to come from the installed build).
    /// </summary>
    private static string? IpcTypeOfFile(string osuGameDll)
    {
        try
        {
            var (context, assembly) = LoadInstalled(osuGameDll);
            using (context)
            {
                return assembly.GetType("osu.Game.IPC.ArchiveImportMessage")?.AssemblyQualifiedName;
            }
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine($"could not read the IPC message type from {osuGameDll}: {ex.Message}");
            return null;
        }
    }

    private static (MetadataLoadContext context, Assembly assembly) LoadInstalled(string osuGameDll)
    {
        string dir = Path.GetDirectoryName(Path.GetFullPath(osuGameDll))!;
        // Framework assemblies come from the runtime; anything else (osu.Game,
        // osu.Framework, Realm, ...) from the install folder. Keyed by file name so
        // two different copies of the same assembly identity can't both be added
        // (MetadataLoadContext refuses duplicate identities).
        var byName = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        foreach (string file in Directory.GetFiles(RuntimeEnvironment.GetRuntimeDirectory(), "*.dll"))
            byName.TryAdd(Path.GetFileName(file), file);
        foreach (string file in Directory.GetFiles(dir, "*.dll"))
            byName.TryAdd(Path.GetFileName(file), file);

        var context = new MetadataLoadContext(new PathAssemblyResolver(byName.Values));
        var assembly = context.LoadFromAssemblyPath(Path.GetFullPath(osuGameDll));
        return (context, assembly);
    }
}
