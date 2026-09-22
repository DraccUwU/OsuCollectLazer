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
//   LazerDb --data-dir <lazer data dir> [--collection <collection.db>] [--dry-run]

using System.Reflection;
using System.Runtime.InteropServices;
using System.Text.Json;
using osu.Framework.Platform;
using osu.Game.Collections;
using osu.Game.Database;

namespace LazerDb;

internal static class Program
{
    private const BindingFlags const_flags = BindingFlags.NonPublic | BindingFlags.Static;

    private static readonly JsonSerializerOptions json_options = new() { WriteIndented = true };

    private static int Main(string[] args)
    {
        string? dataDir = null;
        string? collectionPath = null;
        string? versionsFor = null;
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
                case "--versions-for":
                    versionsFor = Next(args, ref i);
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

            using var context = new MetadataLoadContext(new PathAssemblyResolver(byName.Values));
            var assembly = context.LoadFromAssemblyPath(Path.GetFullPath(osuGameDll));
            return assembly.GetType("osu.Game.Database.RealmAccess")
                           ?.GetField("schema_version", const_flags)
                           ?.GetRawConstantValue()?.ToString();
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine($"could not read schema version from {osuGameDll}: {ex.Message}");
            return null;
        }
    }
}
