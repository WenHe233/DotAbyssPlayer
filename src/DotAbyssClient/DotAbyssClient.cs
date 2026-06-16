using System.Net.Http.Headers;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Web;

namespace DotAbyssClient;

internal sealed class DotAbyssClient : IDisposable
{
    private readonly HttpClient _httpClient;
    private readonly byte[] _appKeyBytes;

    public DotAbyssClient(string appKey)
    {
        _appKeyBytes = DecodeAppKey(appKey);
        _httpClient = new HttpClient(new SocketsHttpHandler
        {
            PooledConnectionLifetime = TimeSpan.FromMinutes(10),
            MaxConnectionsPerServer = 16
        })
        {
            Timeout = TimeSpan.FromMinutes(10)
        };
        _httpClient.DefaultRequestHeaders.UserAgent.ParseAdd("DotAbyssClient/1.0");
        _httpClient.DefaultRequestHeaders.Accept.Add(new MediaTypeWithQualityHeaderValue("application/json"));
    }

    public async Task<MaintenanceResponse> GetMaintenanceAsync(AssetProfile profile, string appVersion, CancellationToken cancellationToken = default)
    {
        var url = profile.ApiRoot + "maintenance?app_version=" + Uri.EscapeDataString(appVersion);
        Console.WriteLine($"GET {url}");
        using var response = await _httpClient.GetAsync(url, cancellationToken);
        var bytes = await response.Content.ReadAsByteArrayAsync(cancellationToken);
        response.EnsureSuccessStatusCode();

        var jsonBytes = TryParseJsonObject(bytes, out var directJson)
            ? bytes
            : DecryptApiBody(bytes, GetRequiredHeader(response, "X-Olg-Session"));

        var raw = directJson ?? JsonNode.Parse(jsonBytes) as JsonObject
            ?? throw new InvalidDataException("Maintenance response was not a JSON object.");
        return new MaintenanceResponse(raw);
    }

    public async Task<CatalogFetchResult> FetchCatalogAsync(
        AssetProfile profile,
        string version,
        string outputDirectory,
        string catalogName,
        bool overwrite,
        CancellationToken cancellationToken = default)
    {
        Directory.CreateDirectory(outputDirectory);

        var hashUrl = profile.BuildCatalogUrl(version, catalogName, ".hash");
        var binUrl = profile.BuildCatalogUrl(version, catalogName, ".bin");
        var hashPath = Path.Combine(outputDirectory, catalogName + ".hash");
        var binPath = Path.Combine(outputDirectory, catalogName + ".bin");

        Console.WriteLine($"Catalog hash URL: {hashUrl}");
        Console.WriteLine($"Catalog bin URL: {binUrl}");

        if (overwrite || !File.Exists(hashPath))
            await DownloadFileAsync(hashUrl, hashPath, cancellationToken);
        else
            Console.WriteLine($"Skip existing catalog hash: {hashPath}");

        if (overwrite || !File.Exists(binPath))
            await DownloadFileAsync(binUrl, binPath, cancellationToken);
        else
            Console.WriteLine($"Skip existing catalog bin: {binPath}");

        var hash = (await File.ReadAllTextAsync(hashPath, cancellationToken)).Trim();
        return new CatalogFetchResult(binPath, hashPath, hash, profile.BuildAddressablesBaseUrl(version));
    }

    private async Task DownloadFileAsync(string url, string path, CancellationToken cancellationToken)
    {
        Console.WriteLine($"Downloading {url}");
        using var response = await _httpClient.GetAsync(url, HttpCompletionOption.ResponseHeadersRead, cancellationToken);
        response.EnsureSuccessStatusCode();
        Directory.CreateDirectory(Path.GetDirectoryName(Path.GetFullPath(path))!);
        await using var input = await response.Content.ReadAsStreamAsync(cancellationToken);
        await using var output = new FileStream(path, FileMode.Create, FileAccess.Write, FileShare.None, 1024 * 1024, FileOptions.Asynchronous | FileOptions.SequentialScan);
        await input.CopyToAsync(output, cancellationToken);
        Console.WriteLine($"  wrote {path}");
    }

    public void Dispose()
    {
        _httpClient.Dispose();
    }

    private byte[] DecryptApiBody(byte[] encryptedBody, string encryptedSessionHeader)
    {
        var session = DecryptLaravelString(encryptedSessionHeader);
        var aesKey = HMACSHA256.HashData(_appKeyBytes, Encoding.UTF8.GetBytes(session));
        if (encryptedBody.Length < 17)
            throw new InvalidDataException("Encrypted API body is too short.");

        var iv = encryptedBody.AsSpan(0, 16).ToArray();
        var cipherText = encryptedBody.AsSpan(16).ToArray();
        using var aes = Aes.Create();
        aes.Key = aesKey;
        aes.IV = iv;
        aes.Mode = CipherMode.CBC;
        aes.Padding = PaddingMode.PKCS7;
        using var decryptor = aes.CreateDecryptor();
        return decryptor.TransformFinalBlock(cipherText, 0, cipherText.Length);
    }

    private string DecryptLaravelString(string payload)
    {
        var decodedPayload = HttpUtility.UrlDecode(payload);
        var payloadJson = Encoding.UTF8.GetString(Convert.FromBase64String(decodedPayload));
        var node = JsonNode.Parse(payloadJson) as JsonObject
            ?? throw new InvalidDataException("Laravel payload was not a JSON object.");
        var ivText = node["iv"]?.GetValue<string>() ?? throw new InvalidDataException("Laravel payload has no iv.");
        var valueText = node["value"]?.GetValue<string>() ?? throw new InvalidDataException("Laravel payload has no value.");
        var mac = node["mac"]?.GetValue<string>() ?? string.Empty;

        var expectedMac = Convert.ToHexString(HMACSHA256.HashData(_appKeyBytes, Encoding.UTF8.GetBytes(ivText + valueText))).ToLowerInvariant();
        if (!string.IsNullOrEmpty(mac) && !CryptographicOperations.FixedTimeEquals(Encoding.ASCII.GetBytes(expectedMac), Encoding.ASCII.GetBytes(mac)))
            throw new InvalidDataException("Laravel payload MAC validation failed.");

        var iv = Convert.FromBase64String(ivText);
        var cipherText = Convert.FromBase64String(valueText);
        using var aes = Aes.Create();
        aes.Key = _appKeyBytes;
        aes.IV = iv;
        aes.Mode = CipherMode.CBC;
        aes.Padding = PaddingMode.PKCS7;
        using var decryptor = aes.CreateDecryptor();
        var plain = decryptor.TransformFinalBlock(cipherText, 0, cipherText.Length);
        return ParsePhpSerializedString(Encoding.UTF8.GetString(plain));
    }

    private static string ParsePhpSerializedString(string value)
    {
        if (!value.StartsWith("s:", StringComparison.Ordinal))
            return value;
        var firstColon = value.IndexOf(':', 2);
        var firstQuote = value.IndexOf('"', firstColon + 1);
        var lastQuote = value.LastIndexOf('"');
        if (firstColon < 0 || firstQuote < 0 || lastQuote <= firstQuote)
            throw new InvalidDataException("Unsupported PHP serialized string payload.");
        return value[(firstQuote + 1)..lastQuote];
    }

    private static bool TryParseJsonObject(byte[] bytes, out JsonObject? json)
    {
        try
        {
            json = JsonNode.Parse(bytes) as JsonObject;
            return json != null;
        }
        catch (JsonException)
        {
            json = null;
            return false;
        }
    }

    private static string GetRequiredHeader(HttpResponseMessage response, string name)
    {
        if (response.Headers.TryGetValues(name, out var values))
            return values.First();
        throw new InvalidDataException($"Response did not include required header {name}.");
    }

    private static byte[] DecodeAppKey(string appKey)
    {
        const string prefix = "base64:";
        if (appKey.StartsWith(prefix, StringComparison.OrdinalIgnoreCase))
            return Convert.FromBase64String(appKey[prefix.Length..]);
        return Encoding.UTF8.GetBytes(appKey);
    }
}

internal sealed class MaintenanceResponse
{
    public MaintenanceResponse(JsonObject raw)
    {
        Raw = raw;
    }

    public JsonObject Raw { get; }

    public string GetVersion(string key)
    {
        var versions = Raw["versions"] as JsonObject
            ?? throw new InvalidDataException("Maintenance response has no versions object.");
        var values = versions[key] as JsonArray
            ?? throw new InvalidDataException($"Maintenance response has no version key '{key}'.");
        var version = values.Select(v => v?.GetValue<string>()).FirstOrDefault(v => !string.IsNullOrWhiteSpace(v));
        if (string.IsNullOrWhiteSpace(version))
            throw new InvalidDataException($"Maintenance version key '{key}' was empty.");
        return version;
    }
}

internal sealed record CatalogFetchResult(string CatalogPath, string HashPath, string Hash, string BaseUrl);
