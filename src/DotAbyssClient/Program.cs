using System.Globalization;
using System.Text.Json;

namespace DotAbyssClient;

internal static class Program
{
    public static async Task<int> Main(string[] args)
    {
        try
        {
            var options = AppOptions.Parse(args);
            if (options.ShowHelp)
            {
                AppOptions.PrintUsage();
                return 0;
            }

            var profile = AssetProfile.FromName(options.ProfileName);
            var outputRoot = options.OutputDirectory ?? Path.Combine("workspace", "bundles", profile.Name);
            var catalogDir = options.CatalogDirectory ?? Path.Combine(outputRoot, "_catalog");

            using var client = new DotAbyssClient(options.AppKey);
            var version = options.Version;
            if (string.IsNullOrWhiteSpace(version))
            {
                var maintenance = await client.GetMaintenanceAsync(profile, options.AppVersion);
                version = maintenance.GetVersion(profile.AssetVersionKey);
                Console.WriteLine($"Resolved {profile.AssetVersionKey}: {version}");
                Directory.CreateDirectory(catalogDir);
                await File.WriteAllTextAsync(
                    Path.Combine(catalogDir, "maintenance.json"),
                    JsonSerializer.Serialize(maintenance.Raw, JsonOptions.Pretty));
            }

            var baseUrl = profile.BuildAddressablesBaseUrl(version);
            var catalogInfo = await client.FetchCatalogAsync(
                profile,
                version,
                catalogDir,
                options.CatalogName,
                options.OverwriteCatalog);

            Console.WriteLine($"Reading catalog: {catalogInfo.CatalogPath}");
            var catalog = CatalogParser.Parse(catalogInfo.CatalogPath);
            CatalogWriter.WriteSummary(catalog, Path.Combine(catalogDir, $"{options.CatalogName}.summary.json"));
            if (options.WriteCatalogJson)
                CatalogWriter.WriteJson(catalog, Path.Combine(catalogDir, $"{options.CatalogName}.extracted.json"));

            var manager = new AssetManager(new AssetDownloadSettings
            {
                OutputDirectory = outputRoot,
                ParallelDownloads = options.ParallelDownloads,
                Retries = options.Retries,
                Overwrite = options.OverwriteBundles,
                IncludeLocal = options.IncludeLocal
            });

            var bundles = manager.BuildBundleList(catalog, baseUrl);
            if (options.Limit is { } limit && limit < bundles.Count)
                bundles = bundles.Take(limit).ToList();

            Directory.CreateDirectory(outputRoot);
            var manifestPath = Path.Combine(outputRoot, "download_manifest.tsv");
            AssetManager.WriteManifest(bundles, manifestPath);

            var totalBytes = bundles.Sum(b => b.ExpectedSize);
            Console.WriteLine($"Profile: {profile.Name}");
            Console.WriteLine($"Base URL: {baseUrl}");
            Console.WriteLine($"Catalog hash: {catalogInfo.Hash}");
            Console.WriteLine($"Bundles: {bundles.Count.ToString(CultureInfo.InvariantCulture)}");
            Console.WriteLine($"Expected size: {Format.Bytes(totalBytes)}");
            Console.WriteLine($"Output: {outputRoot}");
            Console.WriteLine($"Manifest: {manifestPath}");

            if (options.DryRun)
                return 0;

            var summary = await manager.DownloadAsync(bundles);
            Console.WriteLine($"Downloaded: {summary.Downloaded.ToString(CultureInfo.InvariantCulture)}");
            Console.WriteLine($"Skipped: {summary.Skipped.ToString(CultureInfo.InvariantCulture)}");
            Console.WriteLine($"Failed: {summary.Failed.ToString(CultureInfo.InvariantCulture)}");
            if (summary.Failed > 0)
                Console.WriteLine($"Failures: {Path.Combine(outputRoot, "download_failures.tsv")}");

            return summary.Failed == 0 ? 0 : 1;
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine(ex.Message);
            Console.Error.WriteLine(ex.StackTrace);
            return 1;
        }
    }
}

internal sealed class AppOptions
{
    public string ProfileName { get; private set; } = "android-dmm-r18";
    public string AppVersion { get; private set; } = "1.1.2";
    public string AppKey { get; private set; } = "base64:b5RHgCQ66Glhlru9WV5Koc5SulPDiWZ44K0+dCeVTn0=";
    public string? Version { get; private set; }
    public string? OutputDirectory { get; private set; }
    public string? CatalogDirectory { get; private set; }
    public string CatalogName { get; private set; } = "catalog_1";
    public int ParallelDownloads { get; private set; } = 8;
    public int Retries { get; private set; } = 3;
    public int? Limit { get; private set; }
    public bool DryRun { get; private set; }
    public bool OverwriteCatalog { get; private set; }
    public bool OverwriteBundles { get; private set; }
    public bool IncludeLocal { get; private set; }
    public bool WriteCatalogJson { get; private set; }
    public bool ShowHelp { get; private set; }

    public static AppOptions Parse(string[] args)
    {
        var options = new AppOptions();
        var start = args.Length > 0 && string.Equals(args[0], "download", StringComparison.OrdinalIgnoreCase) ? 1 : 0;

        for (var i = start; i < args.Length; i++)
        {
            var arg = args[i];
            switch (arg)
            {
                case "-h":
                case "--help":
                    options.ShowHelp = true;
                    break;
                case "--profile":
                    options.ProfileName = RequireValue(arg, args, ref i);
                    break;
                case "--app-version":
                    options.AppVersion = RequireValue(arg, args, ref i);
                    break;
                case "--app-key":
                    options.AppKey = RequireValue(arg, args, ref i);
                    break;
                case "--version":
                    options.Version = RequireValue(arg, args, ref i);
                    break;
                case "-o":
                case "--out":
                    options.OutputDirectory = RequireValue(arg, args, ref i);
                    break;
                case "--catalog-dir":
                    options.CatalogDirectory = RequireValue(arg, args, ref i);
                    break;
                case "--catalog-name":
                case "--name":
                    options.CatalogName = RequireValue(arg, args, ref i);
                    break;
                case "--parallel":
                    options.ParallelDownloads = RequirePositiveInt(arg, args, ref i);
                    break;
                case "--retries":
                    options.Retries = RequireNonNegativeInt(arg, args, ref i);
                    break;
                case "--limit":
                    options.Limit = RequirePositiveInt(arg, args, ref i);
                    break;
                case "--dry-run":
                    options.DryRun = true;
                    break;
                case "--overwrite-catalog":
                    options.OverwriteCatalog = true;
                    break;
                case "--overwrite":
                case "--overwrite-bundles":
                    options.OverwriteBundles = true;
                    break;
                case "--include-local":
                    options.IncludeLocal = true;
                    break;
                case "--write-catalog-json":
                    options.WriteCatalogJson = true;
                    break;
                default:
                    throw new ArgumentException($"Unknown option: {arg}");
            }
        }

        if (string.IsNullOrWhiteSpace(options.CatalogName))
            throw new ArgumentException("Catalog name cannot be empty.");
        return options;
    }

    public static void PrintUsage()
    {
        Console.WriteLine("Usage:");
        Console.WriteLine("  dotnet run --project src/DotAbyssClient -- download --profile android-dmm-r18 -o workspace/bundles/android-dmm-r18");
        Console.WriteLine();
        Console.WriteLine("Options:");
        Console.WriteLine("  --profile <name>        android-dmm-r18, android-dmm-normal, android-googleplay-normal, webgl-r18, webgl-normal");
        Console.WriteLine("  --app-version <ver>     App version for maintenance lookup. Default: 1.1.2");
        Console.WriteLine("  --app-key <key>         OlgBaseApi_AppKey. Default is production app key from runtime config");
        Console.WriteLine("  --version <n>           Asset version override; skips maintenance lookup");
        Console.WriteLine("  -o, --out <dir>         Output directory for nested bundle files");
        Console.WriteLine("  --catalog-dir <dir>     Catalog cache directory. Default: <out>/_catalog");
        Console.WriteLine("  --parallel <n>          Concurrent bundle downloads. Default: 8");
        Console.WriteLine("  --retries <n>           Retries per file. Default: 3");
        Console.WriteLine("  --dry-run               Fetch/parse catalog and write manifest without downloading bundles");
        Console.WriteLine("  --limit <n>             Download only first n bundles");
        Console.WriteLine("  --overwrite-catalog     Redownload catalog even when cached");
        Console.WriteLine("  --overwrite             Redownload complete bundle files");
        Console.WriteLine("  --write-catalog-json    Also write a full extracted catalog JSON");
    }

    private static string RequireValue(string arg, string[] args, ref int index)
    {
        if (++index >= args.Length || string.IsNullOrWhiteSpace(args[index]))
            throw new ArgumentException($"{arg} requires a value.");
        return args[index];
    }

    private static int RequirePositiveInt(string arg, string[] args, ref int index)
    {
        var value = RequireValue(arg, args, ref index);
        if (!int.TryParse(value, NumberStyles.Integer, CultureInfo.InvariantCulture, out var result) || result < 1)
            throw new ArgumentException($"{arg} requires a positive integer.");
        return result;
    }

    private static int RequireNonNegativeInt(string arg, string[] args, ref int index)
    {
        var value = RequireValue(arg, args, ref index);
        if (!int.TryParse(value, NumberStyles.Integer, CultureInfo.InvariantCulture, out var result) || result < 0)
            throw new ArgumentException($"{arg} requires a non-negative integer.");
        return result;
    }
}

internal static class Format
{
    public static string Bytes(long bytes)
    {
        string[] units = { "B", "KiB", "MiB", "GiB", "TiB" };
        var value = (double)bytes;
        var unit = 0;
        while (value >= 1024 && unit < units.Length - 1)
        {
            value /= 1024;
            unit++;
        }

        return string.Create(CultureInfo.InvariantCulture, $"{value:0.##} {units[unit]}");
    }
}

internal static class JsonOptions
{
    public static readonly JsonSerializerOptions Pretty = new()
    {
        WriteIndented = true,
        PropertyNamingPolicy = JsonNamingPolicy.CamelCase
    };
}
