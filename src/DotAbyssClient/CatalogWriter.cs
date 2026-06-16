using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace DotAbyssClient;

internal static class CatalogWriter
{
    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        WriteIndented = true,
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
        PropertyNamingPolicy = JsonNamingPolicy.CamelCase
    };

    public static void WriteSummary(CatalogFile catalog, string path)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(Path.GetFullPath(path))!);
        var bundleCount = catalog.Locations.Count(IsBundleLocation);
        var summary = new
        {
            source = NormalizePath(catalog.SourcePath),
            sourceLength = catalog.SourceLength,
            magic = catalog.Header.Magic,
            version = catalog.Header.Version,
            buildResultHash = catalog.BuildResultHash,
            keyCount = catalog.Keys.Count,
            locationCount = catalog.Locations.Count,
            bundleLocationCount = bundleCount,
            resourceProviders = catalog.ResourceProviders
        };

        File.WriteAllText(path, JsonSerializer.Serialize(summary, JsonOptions), new UTF8Encoding(false));
    }

    public static void WriteJson(CatalogFile catalog, string path)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(Path.GetFullPath(path))!);

        var locationsByOffset = catalog.Locations.ToDictionary(l => l.Offset);
        var dump = new CatalogDump
        {
            Source = NormalizePath(catalog.SourcePath),
            SourceLength = catalog.SourceLength,
            Magic = catalog.Header.Magic,
            Version = catalog.Header.Version,
            BuildResultHash = catalog.BuildResultHash,
            InstanceProvider = catalog.InstanceProvider,
            SceneProvider = catalog.SceneProvider,
            ResourceProviders = catalog.ResourceProviders,
            KeyCount = catalog.Keys.Count,
            LocationCount = catalog.Locations.Count,
            Keys = catalog.Keys.Select(k => new CatalogKeyDump
            {
                Key = k.KeyDisplay,
                KeyType = k.KeyType,
                KeyValue = ValueFormatter.ToJsonValue(k.Key),
                LocationOffsets = k.LocationOffsets,
                PrimaryKeys = k.PrimaryKeys
            }).ToList(),
            Locations = catalog.Locations
                .OrderBy(l => l.Offset)
                .Select(l => new CatalogLocationDump
                {
                    Offset = l.Offset,
                    PrimaryKey = l.PrimaryKey,
                    ResourceType = l.ResourceType,
                    ProviderId = l.ProviderId,
                    InternalId = l.InternalId,
                    Data = ValueFormatter.ToJsonValue(l.Data),
                    DependencyHashCode = l.DependencyHashCode,
                    DependencyOffsets = l.DependencyOffsets,
                    Dependencies = l.DependencyOffsets
                        .Select(o => locationsByOffset.TryGetValue(o, out var dep) ? dep.PrimaryKey ?? $"@0x{o:X8}" : $"@0x{o:X8}")
                        .ToList()
                }).ToList()
        };

        File.WriteAllText(path, JsonSerializer.Serialize(dump, JsonOptions), new UTF8Encoding(false));
    }

    private static bool IsBundleLocation(CatalogLocation location)
    {
        return location.InternalId != null
            && location.InternalId.EndsWith(".bundle", StringComparison.OrdinalIgnoreCase)
            && string.Equals(location.ResourceType?.FullName, "UnityEngine.ResourceManagement.ResourceProviders.IAssetBundleResource", StringComparison.Ordinal);
    }

    private static string NormalizePath(string path)
    {
        return path.Replace('\\', '/');
    }
}

internal sealed class CatalogDump
{
    public string? Source { get; init; }
    public int SourceLength { get; init; }
    public int Magic { get; init; }
    public int Version { get; init; }
    public string? BuildResultHash { get; init; }
    public ObjectInitializationDataDump? InstanceProvider { get; init; }
    public ObjectInitializationDataDump? SceneProvider { get; init; }
    public List<ObjectInitializationDataDump> ResourceProviders { get; init; } = new();
    public int KeyCount { get; init; }
    public int LocationCount { get; init; }
    public List<CatalogKeyDump> Keys { get; init; } = new();
    public List<CatalogLocationDump> Locations { get; init; } = new();
}

internal sealed class CatalogKeyDump
{
    public string? Key { get; init; }
    public string? KeyType { get; init; }
    public object? KeyValue { get; init; }
    public List<uint> LocationOffsets { get; init; } = new();
    public List<string> PrimaryKeys { get; init; } = new();
}

internal sealed class CatalogLocationDump
{
    public uint Offset { get; init; }
    public string? PrimaryKey { get; init; }
    public TypeRef? ResourceType { get; init; }
    public string? ProviderId { get; init; }
    public string? InternalId { get; init; }
    public object? Data { get; init; }
    public int DependencyHashCode { get; init; }
    public List<uint> DependencyOffsets { get; init; } = new();
    public List<string> Dependencies { get; init; } = new();
}
