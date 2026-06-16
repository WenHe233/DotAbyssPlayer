using System.Globalization;
using System.Net.Http.Headers;
using System.Text;

namespace DotAbyssClient;

internal sealed class AssetManager
{
    private const int CopyBufferSize = 1024 * 1024;
    private readonly AssetDownloadSettings _settings;

    public AssetManager(AssetDownloadSettings settings)
    {
        _settings = settings;
    }

    public List<BundleDownloadItem> BuildBundleList(CatalogFile catalog, string baseUrl)
    {
        var normalizedBaseUrl = AssetProfile.EnsureTrailingSlash(baseUrl);
        var byFileName = new Dictionary<string, BundleDownloadItem>(StringComparer.OrdinalIgnoreCase);

        foreach (var location in catalog.Locations)
        {
            if (!IsBundleLocation(location))
                continue;
            if (!_settings.IncludeLocal && IsLocalLoadPath(location.InternalId))
                continue;

            var remoteRelativePath = GetRelativePathFromInternalId(location.InternalId);
            if (string.IsNullOrWhiteSpace(remoteRelativePath))
                continue;

            var outputRelativePath = BundlePathMapper.ToNestedRelativePath(remoteRelativePath);
            var data = location.Data as AssetBundleRequestOptionsDump;
            var url = CombineUrl(normalizedBaseUrl, remoteRelativePath);
            var outputPath = Path.Combine(_settings.OutputDirectory, outputRelativePath.Replace('/', Path.DirectorySeparatorChar));

            if (!byFileName.TryGetValue(remoteRelativePath, out var item))
            {
                byFileName.Add(remoteRelativePath, new BundleDownloadItem
                {
                    RemoteRelativePath = remoteRelativePath,
                    OutputRelativePath = outputRelativePath,
                    Url = url,
                    OutputPath = outputPath,
                    ExpectedSize = data?.BundleSize ?? 0,
                    Hash = data?.Hash,
                    Crc = data?.Crc ?? 0,
                    ProviderId = location.ProviderId
                });
                continue;
            }

            item.ExpectedSize = Math.Max(item.ExpectedSize, data?.BundleSize ?? 0);
            item.Hash ??= data?.Hash;
            if (item.Crc == 0)
                item.Crc = data?.Crc ?? 0;
        }

        return byFileName.Values.OrderBy(i => i.RemoteRelativePath, StringComparer.OrdinalIgnoreCase).ToList();
    }

    public async Task<DownloadSummary> DownloadAsync(IReadOnlyList<BundleDownloadItem> items, CancellationToken cancellationToken = default)
    {
        using var httpClient = new HttpClient(new SocketsHttpHandler
        {
            PooledConnectionLifetime = TimeSpan.FromMinutes(10),
            MaxConnectionsPerServer = Math.Max(_settings.ParallelDownloads, 2)
        })
        {
            Timeout = TimeSpan.FromMinutes(10)
        };
        httpClient.DefaultRequestHeaders.UserAgent.ParseAdd("DotAbyssClient/1.0");

        var completed = 0;
        var downloaded = 0;
        var skipped = 0;
        var failed = 0;
        long completedBytes = 0;
        var totalBytes = items.Sum(i => i.ExpectedSize);
        var failures = new List<DownloadFailure>();
        var failureLock = new object();

        using var timer = new Timer(_ =>
        {
            var done = Volatile.Read(ref completed);
            var okBytes = Interlocked.Read(ref completedBytes);
            Console.Write($"\rDone {done}/{items.Count} | downloaded {Volatile.Read(ref downloaded)} | skipped {Volatile.Read(ref skipped)} | failed {Volatile.Read(ref failed)} | {Format.Bytes(okBytes)}/{Format.Bytes(totalBytes)}   ");
        }, null, TimeSpan.FromSeconds(1), TimeSpan.FromSeconds(2));

        await Parallel.ForEachAsync(
            items,
            new ParallelOptions { MaxDegreeOfParallelism = _settings.ParallelDownloads, CancellationToken = cancellationToken },
            async (item, token) =>
            {
                var result = await DownloadOneAsync(httpClient, item, token);
                Interlocked.Increment(ref completed);
                Interlocked.Add(ref completedBytes, result.CompletedBytes);

                switch (result.Status)
                {
                    case DownloadStatus.Downloaded:
                        Interlocked.Increment(ref downloaded);
                        break;
                    case DownloadStatus.Skipped:
                        Interlocked.Increment(ref skipped);
                        break;
                    case DownloadStatus.Failed:
                        Interlocked.Increment(ref failed);
                        lock (failureLock)
                            failures.Add(new DownloadFailure(item, result.Error ?? "unknown error"));
                        break;
                }
            });

        timer.Change(Timeout.Infinite, Timeout.Infinite);
        Console.WriteLine();

        WriteFailures(failures, Path.Combine(_settings.OutputDirectory, "download_failures.tsv"));
        return new DownloadSummary(downloaded, skipped, failed);
    }

    public static void WriteManifest(IEnumerable<BundleDownloadItem> items, string path)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(Path.GetFullPath(path))!);
        using var writer = new StreamWriter(path, false, new UTF8Encoding(false));
        writer.WriteLine("remoteRelativePath\toutputRelativePath\turl\texpectedSize\thash\tcrc\tproviderId");
        foreach (var item in items)
        {
            writer.Write(Tsv(item.RemoteRelativePath));
            writer.Write('\t');
            writer.Write(Tsv(item.OutputRelativePath));
            writer.Write('\t');
            writer.Write(Tsv(item.Url));
            writer.Write('\t');
            writer.Write(item.ExpectedSize.ToString(CultureInfo.InvariantCulture));
            writer.Write('\t');
            writer.Write(Tsv(item.Hash));
            writer.Write('\t');
            writer.Write(item.Crc.ToString(CultureInfo.InvariantCulture));
            writer.Write('\t');
            writer.WriteLine(Tsv(item.ProviderId));
        }
    }

    private async Task<DownloadResult> DownloadOneAsync(HttpClient httpClient, BundleDownloadItem item, CancellationToken cancellationToken)
    {
        if (!_settings.Overwrite && IsComplete(item.OutputPath, item.ExpectedSize, out var completeLength))
            return DownloadResult.Skipped(completeLength);

        var partPath = item.OutputPath + ".part";
        Directory.CreateDirectory(Path.GetDirectoryName(Path.GetFullPath(item.OutputPath))!);

        Exception? lastError = null;
        for (var attempt = 0; attempt <= _settings.Retries; attempt++)
        {
            try
            {
                var partLength = File.Exists(partPath) ? new FileInfo(partPath).Length : 0;
                if (item.ExpectedSize > 0 && partLength == item.ExpectedSize)
                {
                    PromotePartFile(partPath, item.OutputPath);
                    return DownloadResult.Downloaded(partLength);
                }

                if (item.ExpectedSize > 0 && partLength > item.ExpectedSize)
                {
                    File.Delete(partPath);
                    partLength = 0;
                }

                using var request = new HttpRequestMessage(HttpMethod.Get, item.Url);
                if (partLength > 0)
                    request.Headers.Range = new RangeHeaderValue(partLength, null);

                using var response = await httpClient.SendAsync(request, HttpCompletionOption.ResponseHeadersRead, cancellationToken);
                var append = partLength > 0 && response.StatusCode == System.Net.HttpStatusCode.PartialContent;
                if (partLength > 0 && response.StatusCode == System.Net.HttpStatusCode.OK)
                {
                    File.Delete(partPath);
                    partLength = 0;
                    append = false;
                }

                response.EnsureSuccessStatusCode();
                await using (var input = await response.Content.ReadAsStreamAsync(cancellationToken))
                await using (var output = new FileStream(
                    partPath,
                    append ? FileMode.Append : FileMode.Create,
                    FileAccess.Write,
                    FileShare.None,
                    CopyBufferSize,
                    FileOptions.Asynchronous | FileOptions.SequentialScan))
                {
                    await input.CopyToAsync(output, CopyBufferSize, cancellationToken);
                }

                var downloadedLength = new FileInfo(partPath).Length;
                if (item.ExpectedSize > 0 && downloadedLength != item.ExpectedSize)
                    throw new IOException($"Size mismatch: expected {item.ExpectedSize}, got {downloadedLength}.");

                PromotePartFile(partPath, item.OutputPath);
                return DownloadResult.Downloaded(downloadedLength);
            }
            catch (Exception ex) when (attempt < _settings.Retries)
            {
                lastError = ex;
                var delay = TimeSpan.FromMilliseconds(500 * Math.Pow(2, attempt));
                await Task.Delay(delay, cancellationToken);
            }
            catch (Exception ex)
            {
                lastError = ex;
                break;
            }
        }

        return DownloadResult.Failed(lastError?.Message ?? "unknown error");
    }

    private static bool IsBundleLocation(CatalogLocation location)
    {
        return location.InternalId != null
            && location.InternalId.EndsWith(".bundle", StringComparison.OrdinalIgnoreCase)
            && string.Equals(location.ResourceType?.FullName, "UnityEngine.ResourceManagement.ResourceProviders.IAssetBundleResource", StringComparison.Ordinal);
    }

    private static bool IsLocalLoadPath(string? internalId)
    {
        return internalId != null && internalId.Contains("LocalLoadPath", StringComparison.Ordinal);
    }

    private static string GetRelativePathFromInternalId(string? internalId)
    {
        if (string.IsNullOrWhiteSpace(internalId))
            return string.Empty;

        var normalized = internalId.Replace('\\', '/');
        var queryIndex = normalized.IndexOf('?', StringComparison.Ordinal);
        if (queryIndex >= 0)
            normalized = normalized[..queryIndex];

        var placeholderEnd = normalized.IndexOf("}/", StringComparison.Ordinal);
        if (placeholderEnd >= 0)
            return normalized[(placeholderEnd + 2)..].TrimStart('/');

        if (Uri.TryCreate(normalized, UriKind.Absolute, out var uri))
            return uri.AbsolutePath.TrimStart('/');

        return normalized.TrimStart('/');
    }

    private static string CombineUrl(string baseUrl, string relativePath)
    {
        var escapedPath = string.Join(
            "/",
            relativePath
                .Split('/', StringSplitOptions.RemoveEmptyEntries)
                .Select(Uri.EscapeDataString));
        return AssetProfile.EnsureTrailingSlash(baseUrl) + escapedPath;
    }

    private static bool IsComplete(string path, long expectedSize, out long length)
    {
        if (!File.Exists(path))
        {
            length = 0;
            return false;
        }

        length = new FileInfo(path).Length;
        return expectedSize <= 0 || length == expectedSize;
    }

    private static void PromotePartFile(string partPath, string outputPath)
    {
        if (File.Exists(outputPath))
            File.Delete(outputPath);
        File.Move(partPath, outputPath);
    }

    private static void WriteFailures(IEnumerable<DownloadFailure> failures, string path)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(Path.GetFullPath(path))!);
        using var writer = new StreamWriter(path, false, new UTF8Encoding(false));
        writer.WriteLine("remoteRelativePath\toutputRelativePath\turl\terror");
        foreach (var failure in failures)
        {
            writer.Write(Tsv(failure.Item.RemoteRelativePath));
            writer.Write('\t');
            writer.Write(Tsv(failure.Item.OutputRelativePath));
            writer.Write('\t');
            writer.Write(Tsv(failure.Item.Url));
            writer.Write('\t');
            writer.WriteLine(Tsv(failure.Error));
        }
    }

    private static string Tsv(string? value)
    {
        return (value ?? string.Empty)
            .Replace("\t", " ", StringComparison.Ordinal)
            .Replace("\r", " ", StringComparison.Ordinal)
            .Replace("\n", " ", StringComparison.Ordinal);
    }
}

internal static class BundlePathMapper
{
    private static readonly HashSet<string> ReservedNames = new(StringComparer.OrdinalIgnoreCase)
    {
        "CON", "PRN", "AUX", "NUL",
        "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
        "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9"
    };

    public static string ToNestedRelativePath(string remoteRelativePath)
    {
        var normalized = remoteRelativePath.Replace('\\', '/').Trim('/');
        var prefix = string.Empty;
        var slash = normalized.LastIndexOf('/');
        if (slash >= 0)
        {
            prefix = normalized[..slash];
            normalized = normalized[(slash + 1)..];
        }

        var nestedFile = ToNestedBundleFileName(normalized);
        return string.IsNullOrWhiteSpace(prefix)
            ? nestedFile
            : prefix + "/" + nestedFile;
    }

    private static string ToNestedBundleFileName(string fileName)
    {
        const string bundleExtension = ".bundle";
        if (!fileName.EndsWith(bundleExtension, StringComparison.OrdinalIgnoreCase))
            return SanitizeSegment(fileName);

        var body = fileName[..^bundleExtension.Length];
        var hashSeparator = body.LastIndexOf('_');
        if (hashSeparator <= 0 || hashSeparator == body.Length - 1)
            return SanitizeSegment(fileName);

        var hash = body[(hashSeparator + 1)..];
        var logicalName = body[..hashSeparator];
        var leafSeparator = logicalName.LastIndexOf('_');
        if (leafSeparator <= 0 || leafSeparator == logicalName.Length - 1)
            return SanitizeSegment(logicalName) + "_" + SanitizeSegment(hash) + bundleExtension;

        var dirs = logicalName[..leafSeparator]
            .Split('_', StringSplitOptions.RemoveEmptyEntries)
            .Select(SanitizeSegment);
        var leaf = SanitizeSegment(logicalName[(leafSeparator + 1)..]);
        return string.Join("/", dirs.Append(leaf + "_" + SanitizeSegment(hash) + bundleExtension));
    }

    private static string SanitizeSegment(string value)
    {
        var invalid = Path.GetInvalidFileNameChars();
        var chars = value.Select(c => invalid.Contains(c) || c < 32 ? '_' : c).ToArray();
        var sanitized = new string(chars).Trim(' ', '.');
        if (string.IsNullOrEmpty(sanitized))
            sanitized = "_";
        if (ReservedNames.Contains(sanitized))
            sanitized = "_" + sanitized;
        return sanitized;
    }
}

internal sealed class AssetDownloadSettings
{
    public required string OutputDirectory { get; init; }
    public int ParallelDownloads { get; init; } = 8;
    public int Retries { get; init; } = 3;
    public bool Overwrite { get; init; }
    public bool IncludeLocal { get; init; }
}

internal sealed class BundleDownloadItem
{
    public required string RemoteRelativePath { get; init; }
    public required string OutputRelativePath { get; init; }
    public required string Url { get; init; }
    public required string OutputPath { get; init; }
    public long ExpectedSize { get; set; }
    public string? Hash { get; set; }
    public uint Crc { get; set; }
    public string? ProviderId { get; init; }
}

internal enum DownloadStatus
{
    Downloaded,
    Skipped,
    Failed
}

internal readonly record struct DownloadResult(DownloadStatus Status, long CompletedBytes, string? Error)
{
    public static DownloadResult Downloaded(long completedBytes) => new(DownloadStatus.Downloaded, completedBytes, null);
    public static DownloadResult Skipped(long completedBytes) => new(DownloadStatus.Skipped, completedBytes, null);
    public static DownloadResult Failed(string error) => new(DownloadStatus.Failed, 0, error);
}

internal readonly record struct DownloadFailure(BundleDownloadItem Item, string Error);
internal readonly record struct DownloadSummary(int Downloaded, int Skipped, int Failed);
