using System.Buffers.Binary;
using System.Globalization;
using System.Runtime.CompilerServices;
using System.Runtime.InteropServices;
using System.Text;

namespace DotAbyssClient;

internal static class CatalogParser
{
    private const int Addressables281CatalogMagic = 233015618;
    private const int Addressables281CatalogVersion = 2;

    public static CatalogFile Parse(string path)
    {
        var data = File.ReadAllBytes(path);
        var reader = new BinaryCatalogReader(data);
        var header = reader.ReadValue<CatalogHeader>(0);

        if (header.Magic != Addressables281CatalogMagic)
            throw new InvalidDataException($"Invalid catalog magic in {path}: {header.Magic}. Expected {Addressables281CatalogMagic}.");
        if (header.Version != Addressables281CatalogVersion)
            throw new InvalidDataException($"Unsupported catalog version in {path}: {header.Version}. Expected {Addressables281CatalogVersion}.");

        var catalog = new CatalogFile
        {
            SourcePath = path,
            SourceLength = data.Length,
            Header = header,
            InstanceProvider = reader.ReadObjectInitializationData(header.InstanceProvider),
            SceneProvider = reader.ReadObjectInitializationData(header.SceneProvider),
            ResourceProviders = reader.ReadObjectInitializationDataArray(header.InitObjectsArray).ToList(),
            BuildResultHash = reader.ReadString(header.BuildResultHash)
        };

        var keyDataArray = reader.ReadValueArray<KeyData>(header.KeysOffset);
        foreach (var keyData in keyDataArray)
        {
            var key = reader.ReadObject(keyData.KeyNameOffset);
            var locationOffsets = reader.ReadOffsetArray(keyData.LocationSetOffset);
            var keyEntry = new CatalogKey
            {
                Key = key,
                KeyDisplay = ValueFormatter.Format(key),
                KeyType = ValueFormatter.GetValueTypeName(key),
                LocationOffsets = locationOffsets.ToList()
            };

            foreach (var offset in locationOffsets)
            {
                var location = reader.ReadLocation(offset);
                keyEntry.PrimaryKeys.Add(location.PrimaryKey ?? $"@0x{offset:X8}");
            }

            catalog.Keys.Add(keyEntry);
        }

        reader.ExpandAllDependencies();
        catalog.Locations.AddRange(reader.GetLocations());
        return catalog;
    }
}

internal sealed class BinaryCatalogReader
{
    private const uint UnicodeStringFlag = 0x80000000;
    private const uint DynamicStringFlag = 0x40000000;
    private const uint ClearFlagsMask = 0x3fffffff;

    private readonly byte[] _buffer;
    private readonly Dictionary<StringCacheKey, string?> _stringCache = new();
    private readonly Dictionary<uint, TypeRef?> _typeCache = new();
    private readonly Dictionary<ObjectCacheKey, object?> _objectCache = new();
    private readonly Dictionary<uint, CatalogLocation> _locationCache = new();

    public BinaryCatalogReader(byte[] buffer)
    {
        _buffer = buffer;
    }

    public IReadOnlyCollection<CatalogLocation> GetLocations() => _locationCache.Values;

    public void ExpandAllDependencies()
    {
        var index = 0;
        while (index < _locationCache.Count)
        {
            var location = _locationCache.Values.ElementAt(index++);
            foreach (var dependencyOffset in location.DependencyOffsets)
                ReadLocation(dependencyOffset);
        }
    }

    public T ReadValue<T>(uint offset) where T : unmanaged
    {
        if (offset == uint.MaxValue)
            return default;

        var size = Unsafe.SizeOf<T>();
        EnsureRange(offset, size);
        return MemoryMarshal.Read<T>(_buffer.AsSpan((int)offset, size));
    }

    public T[] ReadValueArray<T>(uint id) where T : unmanaged
    {
        if (id == uint.MaxValue)
            return Array.Empty<T>();

        var byteCount = ReadSizePrefix(id);
        var itemSize = Unsafe.SizeOf<T>();
        if (byteCount % itemSize != 0)
            throw new InvalidDataException($"Array at 0x{id:X8} has {byteCount} bytes, which is not divisible by element size {itemSize}.");

        EnsureRange(id, checked((int)byteCount));
        return MemoryMarshal.Cast<byte, T>(_buffer.AsSpan((int)id, checked((int)byteCount))).ToArray();
    }

    public uint[] ReadOffsetArray(uint id)
    {
        if (id == uint.MaxValue)
            return Array.Empty<uint>();

        var byteCount = ReadSizePrefix(id);
        if (byteCount % sizeof(uint) != 0)
            throw new InvalidDataException($"Offset array at 0x{id:X8} has {byteCount} bytes, which is not divisible by 4.");

        EnsureRange(id, checked((int)byteCount));
        var count = byteCount / sizeof(uint);
        var result = new uint[count];
        for (var i = 0; i < count; i++)
            result[i] = BinaryPrimitives.ReadUInt32LittleEndian(_buffer.AsSpan((int)id + i * sizeof(uint), sizeof(uint)));
        return result;
    }

    public string? ReadString(uint id, char separator = '\0')
    {
        if (id == uint.MaxValue)
            return null;

        var cacheKey = new StringCacheKey(id, separator);
        if (_stringCache.TryGetValue(cacheKey, out var cached))
            return cached;

        var value = separator == '\0'
            ? ReadAutoEncodedString(id)
            : ReadDynamicString(id, separator);

        _stringCache[cacheKey] = value;
        return value;
    }

    public TypeRef? ReadType(uint offset)
    {
        if (offset == uint.MaxValue)
            return null;

        if (_typeCache.TryGetValue(offset, out var cached))
            return cached;

        var data = ReadValue<TypeData>(offset);
        var assembly = ReadString(data.AssemblyId, '.');
        var className = ReadString(data.ClassId, '.');
        var typeRef = string.IsNullOrEmpty(className)
            ? null
            : new TypeRef(assembly, className);
        _typeCache[offset] = typeRef;
        return typeRef;
    }

    public object? ReadObject(uint offset)
    {
        if (offset == uint.MaxValue)
            return null;

        var typeData = ReadValue<ObjectTypeData>(offset);
        var objectType = ReadType(typeData.TypeId);
        return ReadObject(objectType, typeData.ObjectId);
    }

    public ObjectInitializationDataDump? ReadObjectInitializationData(uint offset)
    {
        if (offset == uint.MaxValue)
            return null;

        var data = ReadValue<ObjectInitializationDataRaw>(offset);
        return new ObjectInitializationDataDump
        {
            Id = ReadString(data.Id),
            ObjectType = ReadType(data.Type),
            Data = ReadString(data.Data)
        };
    }

    public IEnumerable<ObjectInitializationDataDump> ReadObjectInitializationDataArray(uint id)
    {
        foreach (var offset in ReadOffsetArray(id))
        {
            var item = ReadObjectInitializationData(offset);
            if (item != null)
                yield return item;
        }
    }

    public CatalogLocation ReadLocation(uint offset)
    {
        if (offset == uint.MaxValue)
            throw new InvalidDataException("A resource location offset cannot be uint.MaxValue.");

        if (_locationCache.TryGetValue(offset, out var cached))
            return cached;

        var raw = ReadValue<ResourceLocationRaw>(offset);
        var location = new CatalogLocation
        {
            Offset = offset,
            ProviderId = ReadString(raw.ProviderOffset, '.'),
            PrimaryKey = ReadString(raw.PrimaryKeyOffset, '/'),
            InternalId = ReadString(raw.InternalIdOffset, '/'),
            ResourceType = ReadType(raw.TypeId),
            Data = ReadObject(raw.ExtraDataOffset),
            DependencySetOffset = raw.DependencySetOffset
        };

        _locationCache[offset] = location;
        location.DependencyOffsets.AddRange(ReadOffsetArray(raw.DependencySetOffset));
        return location;
    }

    private object? ReadObject(TypeRef? type, uint offset)
    {
        if (offset == uint.MaxValue || type == null)
            return null;

        var cacheKey = new ObjectCacheKey(offset, type.FullName ?? string.Empty);
        if (_objectCache.TryGetValue(cacheKey, out var cached))
            return cached;

        object? value = type.FullName switch
        {
            "System.String" => ReadStringObject(offset),
            "System.Int32" => BinaryPrimitives.ReadInt32LittleEndian(ReadSpan(offset, sizeof(int))),
            "System.Boolean" => ReadSpan(offset, 1)[0] != 0,
            "System.Int64" => BinaryPrimitives.ReadInt64LittleEndian(ReadSpan(offset, sizeof(long))),
            "UnityEngine.Hash128" => ReadValue<UnityHash128>(offset).ToString(),
            "System.Type" => ReadType(offset),
            "System.RuntimeType" => ReadType(offset),
            "UnityEngine.ResourceManagement.ResourceProviders.AssetBundleRequestOptions" => ReadAssetBundleRequestOptions(offset),
            "UnityEngine.ResourceManagement.Util.ObjectInitializationData" => ReadObjectInitializationData(offset),
            _ => new UnknownSerializedObject(type, offset)
        };

        _objectCache[cacheKey] = value;
        return value;
    }

    private string? ReadStringObject(uint offset)
    {
        if (offset == uint.MaxValue)
            return null;

        var remap = ReadValue<ObjectToStringRemap>(offset);
        return ReadString(remap.StringId, remap.Separator);
    }

    private AssetBundleRequestOptionsDump ReadAssetBundleRequestOptions(uint offset)
    {
        var data = ReadValue<AssetBundleRequestOptionsRaw>(offset);
        var common = ReadValue<AssetBundleRequestOptionsCommonRaw>(data.CommonId);
        var hash = ReadValue<UnityHash128>(data.HashId).ToString();

        return new AssetBundleRequestOptionsDump
        {
            Hash = hash,
            BundleName = ReadString(data.BundleNameId, '_'),
            Crc = data.Crc,
            BundleSize = data.BundleSize,
            Timeout = common.Timeout,
            RedirectLimit = common.RedirectLimit,
            RetryCount = common.RetryCount,
            AssetLoadMode = (common.Flags & 1) == 1 ? "AllPackedAssetsAndDependencies" : "RequestedAssetAndDependencies",
            ChunkedTransfer = (common.Flags & 2) == 2,
            UseCrcForCachedBundle = (common.Flags & 4) == 4,
            UseUnityWebRequestForLocalBundles = (common.Flags & 8) == 8,
            ClearOtherCachedVersionsWhenLoaded = (common.Flags & 16) == 16
        };
    }

    private string ReadDynamicString(uint id, char separator)
    {
        if ((id & DynamicStringFlag) != DynamicStringFlag)
            return ReadAutoEncodedString(id);

        var parts = new List<string>();
        var nextId = id;
        while (nextId != uint.MaxValue)
        {
            var dynamicString = ReadValue<DynamicString>((uint)(nextId & ClearFlagsMask));
            parts.Add(ReadAutoEncodedString(dynamicString.StringId));
            nextId = dynamicString.NextId;
        }

        parts.Reverse();
        return string.Join(separator, parts);
    }

    private string ReadAutoEncodedString(uint id)
    {
        return (id & UnicodeStringFlag) == UnicodeStringFlag
            ? ReadStringInternal((uint)(id & ClearFlagsMask), Encoding.Unicode)
            : ReadStringInternal(id, Encoding.ASCII);
    }

    private string ReadStringInternal(uint offset, Encoding encoding)
    {
        var byteCount = ReadSizePrefix(offset);
        EnsureRange(offset, checked((int)byteCount));
        return encoding.GetString(_buffer, (int)offset, checked((int)byteCount));
    }

    private uint ReadSizePrefix(uint id)
    {
        if (id < sizeof(uint))
            throw new InvalidDataException($"Size-prefixed data id 0x{id:X8} is invalid.");

        EnsureRange(id - sizeof(uint), sizeof(uint));
        return BinaryPrimitives.ReadUInt32LittleEndian(_buffer.AsSpan((int)id - sizeof(uint), sizeof(uint)));
    }

    private ReadOnlySpan<byte> ReadSpan(uint offset, int size)
    {
        EnsureRange(offset, size);
        return _buffer.AsSpan((int)offset, size);
    }

    private void EnsureRange(uint offset, int size)
    {
        if (size < 0 || offset > int.MaxValue || offset + (uint)size > _buffer.Length)
            throw new InvalidDataException($"Offset 0x{offset:X8}, size {size}, is out of bounds for buffer length {_buffer.Length}.");
    }

    private readonly record struct ObjectCacheKey(uint Offset, string TypeFullName);
    private readonly record struct StringCacheKey(uint Offset, char Separator);
}

internal static class ValueFormatter
{
    public static string GetValueTypeName(object? value)
    {
        return value switch
        {
            null => "null",
            TypeRef => "System.Type",
            UnknownSerializedObject unknown => unknown.Type.FullName ?? "unknown",
            _ => value.GetType().FullName ?? value.GetType().Name
        };
    }

    public static string Format(object? value)
    {
        return value switch
        {
            null => string.Empty,
            string s => s,
            TypeRef type => type.ToString(),
            AssetBundleRequestOptionsDump options => options.ToString(),
            ObjectInitializationDataDump init => init.ToString(),
            UnknownSerializedObject unknown => unknown.ToString(),
            bool b => b ? "true" : "false",
            IFormattable formattable => formattable.ToString(null, CultureInfo.InvariantCulture),
            _ => value.ToString() ?? string.Empty
        };
    }

    public static object? ToJsonValue(object? value)
    {
        return value switch
        {
            null => null,
            TypeRef type => type,
            AssetBundleRequestOptionsDump options => options,
            ObjectInitializationDataDump init => init,
            UnknownSerializedObject unknown => unknown,
            _ => value
        };
    }
}

internal sealed class CatalogFile
{
    public required string SourcePath { get; init; }
    public required int SourceLength { get; init; }
    public required CatalogHeader Header { get; init; }
    public string? BuildResultHash { get; init; }
    public ObjectInitializationDataDump? InstanceProvider { get; init; }
    public ObjectInitializationDataDump? SceneProvider { get; init; }
    public List<ObjectInitializationDataDump> ResourceProviders { get; init; } = new();
    public List<CatalogKey> Keys { get; } = new();
    public List<CatalogLocation> Locations { get; } = new();
}

internal sealed class CatalogKey
{
    public object? Key { get; init; }
    public string KeyDisplay { get; init; } = string.Empty;
    public string KeyType { get; init; } = string.Empty;
    public List<uint> LocationOffsets { get; init; } = new();
    public List<string> PrimaryKeys { get; } = new();
}

internal sealed class CatalogLocation
{
    public required uint Offset { get; init; }
    public string? PrimaryKey { get; init; }
    public string? ProviderId { get; init; }
    public string? InternalId { get; init; }
    public TypeRef? ResourceType { get; init; }
    public object? Data { get; init; }
    public required uint DependencySetOffset { get; init; }
    public List<uint> DependencyOffsets { get; } = new();
    public int DependencyHashCode => unchecked((int)DependencySetOffset);
}

internal sealed class TypeRef
{
    public TypeRef(string? assemblyName, string? fullName)
    {
        AssemblyName = assemblyName;
        FullName = fullName;
    }

    public string? AssemblyName { get; }
    public string? FullName { get; }

    public override string ToString() => FullName ?? "<none>";
}

internal sealed class AssetBundleRequestOptionsDump
{
    public string? Hash { get; init; }
    public string? BundleName { get; init; }
    public uint Crc { get; init; }
    public uint BundleSize { get; init; }
    public short Timeout { get; init; }
    public byte RedirectLimit { get; init; }
    public byte RetryCount { get; init; }
    public string? AssetLoadMode { get; init; }
    public bool ChunkedTransfer { get; init; }
    public bool UseCrcForCachedBundle { get; init; }
    public bool UseUnityWebRequestForLocalBundles { get; init; }
    public bool ClearOtherCachedVersionsWhenLoaded { get; init; }

    public override string ToString()
    {
        return string.Create(
            CultureInfo.InvariantCulture,
            $"AssetBundleRequestOptions(Hash={Hash}, BundleName={BundleName}, Crc={Crc}, BundleSize={BundleSize}, Timeout={Timeout}, RetryCount={RetryCount}, RedirectLimit={RedirectLimit}, AssetLoadMode={AssetLoadMode}, ChunkedTransfer={ChunkedTransfer}, UseCrcForCachedBundle={UseCrcForCachedBundle}, UseUnityWebRequestForLocalBundles={UseUnityWebRequestForLocalBundles}, ClearOtherCachedVersionsWhenLoaded={ClearOtherCachedVersionsWhenLoaded})");
    }
}

internal sealed class ObjectInitializationDataDump
{
    public string? Id { get; init; }
    public TypeRef? ObjectType { get; init; }
    public string? Data { get; init; }

    public override string ToString()
    {
        var dataPart = string.IsNullOrEmpty(Data) ? string.Empty : $", data={Data}";
        return $"ObjectInitializationData(id={Id}, type={ObjectType}{dataPart})";
    }
}

internal sealed class UnknownSerializedObject
{
    public UnknownSerializedObject(TypeRef type, uint offset)
    {
        Type = type;
        Offset = offset;
    }

    public TypeRef Type { get; }
    public uint Offset { get; }

    public override string ToString() => $"<unknown serialized object type={Type} offset=0x{Offset:X8}>";
}

[StructLayout(LayoutKind.Sequential)]
internal struct CatalogHeader
{
    public int Magic;
    public int Version;
    public uint KeysOffset;
    public uint IdOffset;
    public uint InstanceProvider;
    public uint SceneProvider;
    public uint InitObjectsArray;
    public uint BuildResultHash;
}

[StructLayout(LayoutKind.Sequential)]
internal struct KeyData
{
    public uint KeyNameOffset;
    public uint LocationSetOffset;
}

[StructLayout(LayoutKind.Sequential)]
internal struct ResourceLocationRaw
{
    public uint PrimaryKeyOffset;
    public uint InternalIdOffset;
    public uint ProviderOffset;
    public uint DependencySetOffset;
    public int DependencyHashValue;
    public uint ExtraDataOffset;
    public uint TypeId;
}

[StructLayout(LayoutKind.Sequential)]
internal struct ObjectToStringRemap
{
    public uint StringId;
    public char Separator;
}

[StructLayout(LayoutKind.Sequential)]
internal struct DynamicString
{
    public uint StringId;
    public uint NextId;
}

[StructLayout(LayoutKind.Sequential)]
internal struct ObjectTypeData
{
    public uint TypeId;
    public uint ObjectId;
}

[StructLayout(LayoutKind.Sequential)]
internal struct TypeData
{
    public uint AssemblyId;
    public uint ClassId;
}

[StructLayout(LayoutKind.Sequential)]
internal struct ObjectInitializationDataRaw
{
    public uint Id;
    public uint Type;
    public uint Data;
}

[StructLayout(LayoutKind.Sequential)]
internal struct AssetBundleRequestOptionsRaw
{
    public uint HashId;
    public uint BundleNameId;
    public uint Crc;
    public uint BundleSize;
    public uint CommonId;
}

[StructLayout(LayoutKind.Sequential)]
internal struct AssetBundleRequestOptionsCommonRaw
{
    public short Timeout;
    public byte RedirectLimit;
    public byte RetryCount;
    public int Flags;
}

[StructLayout(LayoutKind.Sequential)]
internal struct UnityHash128
{
    public uint U0;
    public uint U1;
    public uint U2;
    public uint U3;

    public override string ToString()
    {
        Span<char> chars = stackalloc char[32];
        WriteUInt32BytesAsHex(U0, chars, 0);
        WriteUInt32BytesAsHex(U1, chars, 8);
        WriteUInt32BytesAsHex(U2, chars, 16);
        WriteUInt32BytesAsHex(U3, chars, 24);
        return new string(chars);
    }

    private static void WriteUInt32BytesAsHex(uint value, Span<char> chars, int offset)
    {
        WriteByteAsHex((byte)value, chars, offset);
        WriteByteAsHex((byte)(value >> 8), chars, offset + 2);
        WriteByteAsHex((byte)(value >> 16), chars, offset + 4);
        WriteByteAsHex((byte)(value >> 24), chars, offset + 6);
    }

    private static void WriteByteAsHex(byte value, Span<char> chars, int offset)
    {
        const string hex = "0123456789abcdef";
        chars[offset] = hex[value >> 4];
        chars[offset + 1] = hex[value & 0xF];
    }
}
