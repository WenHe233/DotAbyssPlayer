using System.Text;

namespace DotAbyssClient;

internal sealed class AssetProfile
{
    private AssetProfile(string name, string apiRoot, string resourceRoot, string platform, string rating, string assetVersionKey)
    {
        Name = name;
        ApiRoot = EnsureTrailingSlash(apiRoot);
        ResourceRoot = EnsureTrailingSlash(resourceRoot);
        Platform = platform;
        Rating = rating;
        AssetVersionKey = assetVersionKey;
    }

    public string Name { get; }
    public string ApiRoot { get; }
    public string ResourceRoot { get; }
    public string Platform { get; }
    public string Rating { get; }
    public string AssetVersionKey { get; }

    public string BuildAddressablesBaseUrl(string version)
    {
        return JoinUrlSegments(ResourceRoot, Platform, Rating, "aas", version, "aa");
    }

    public string BuildCatalogUrl(string version, string catalogName, string extension)
    {
        return BuildAddressablesBaseUrl(version) + Uri.EscapeDataString(catalogName) + extension;
    }

    public static AssetProfile FromName(string name)
    {
        return name.ToLowerInvariant() switch
        {
            "android-dmm-r18" or "android-r18" => new AssetProfile(
                "android-dmm-r18",
                "https://api.abyss-prod-r18.dotabyss.dmmgames.com/",
                "https://api.abyss-prod-r18.dotabyss.dmmgames.com/resources/",
                "android",
                "r18",
                "AssetVersionAndroidDmmR18"),
            "android-dmm-normal" or "android-normal" => new AssetProfile(
                "android-dmm-normal",
                "https://api.abyss-prod.dotabyss.dmmgames.com/",
                "https://api.abyss-prod.dotabyss.dmmgames.com/resources/",
                "android",
                "normal",
                "AssetVersionAndroidDmmGeneral"),
            "android-googleplay-normal" or "android-googleplay" => new AssetProfile(
                "android-googleplay-normal",
                "https://api.abyss-prod.dotabyss.dmmgames.com/",
                "https://api.abyss-prod.dotabyss.dmmgames.com/resources/",
                "android_googleplay",
                "normal",
                "AssetVersionAndroidGooglePlayGeneral"),
            "webgl-r18" or "web-dmm-r18" => new AssetProfile(
                "webgl-r18",
                "https://api.abyss-prod-r18.dotabyss.dmmgames.com/",
                "https://api.abyss-prod-r18.dotabyss.dmmgames.com/resources/",
                "webgl",
                "r18",
                "AssetVersionWebDmmR18"),
            "webgl-normal" or "web-dmm-normal" => new AssetProfile(
                "webgl-normal",
                "https://api.abyss-prod.dotabyss.dmmgames.com/",
                "https://api.abyss-prod.dotabyss.dmmgames.com/resources/",
                "webgl",
                "normal",
                "AssetVersionWebDmmGeneral"),
            _ => throw new ArgumentException($"Unknown profile '{name}'.")
        };
    }

    public static string EnsureTrailingSlash(string url)
    {
        return url.EndsWith("/", StringComparison.Ordinal) ? url : url + "/";
    }

    private static string JoinUrlSegments(params string[] segments)
    {
        var builder = new StringBuilder(EnsureTrailingSlash(segments[0].Trim()));
        foreach (var segment in segments.Skip(1))
        {
            var cleaned = segment.Trim('/');
            if (cleaned.Length == 0)
                continue;
            builder.Append(Uri.EscapeDataString(cleaned).Replace("%2F", "/", StringComparison.Ordinal));
            builder.Append('/');
        }

        return builder.ToString();
    }
}
