using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using CUE4Parse.UE4.Assets.Exports.Texture;
using CUE4Parse.UE4.Objects.Core.Serialization;
using CUE4Parse.UE4.Versions;
using Newtonsoft.Json.Linq;

namespace FModel.Export;

public sealed record SessionConfig
{
    public required string InputDirectory { get; init; }
    public required string Mapping { get; init; }
    public required string DataDirectory { get; init; }
    public EGame Game { get; init; }
    public ETexturePlatform Platform { get; init; }
    public bool ReadScriptData { get; init; }
    public bool ReadShaderMaps { get; init; }
    public bool HashInputs { get; init; }
    public string[] ExtraDirectories { get; init; } = [];
    [Newtonsoft.Json.JsonIgnore] public Dictionary<string, string> Keys { get; init; } = [];
    public FCustomVersion[] CustomVersions { get; init; } = [];
    public Dictionary<string, bool>? VersionOptions { get; init; }
    public Dictionary<string, KeyValuePair<string, string>>? MapStructTypes { get; init; }
    public VersionContainer Versions() => new(Game, Platform,
        customVersions: new FCustomVersionContainer(CustomVersions), optionOverrides: VersionOptions,
        mapStructTypesOverrides: MapStructTypes);
}

public static class ConfigLoader
{
    public static string DefaultGuiPath => Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "FModel", "AppSettings.json");
    public static JObject Read(string path) => JObject.Parse(File.ReadAllText(Path.GetFullPath(path)));
    public static object[] Profiles(string guiPath, string? profileFile)
    {
        var items = new List<object>();
        if (File.Exists(guiPath))
        {
            var root = Read(guiPath);
            foreach (var entry in ((JObject?)root["PerDirectory"] ?? new JObject()).Properties())
                items.Add(new { name = entry.Value.Value<string>("GameName") ?? entry.Name, gameDirectory = entry.Name, source = guiPath });
        }
        if (profileFile is not null)
            foreach (var entry in ((JObject?)Read(profileFile)["profiles"] ?? throw new ArgumentException("profile-file requires a profiles object.")).Properties())
                items.Add(new { name = entry.Name, source = Path.GetFullPath(profileFile) });
        return items.ToArray();
    }
    public static SessionConfig Load(string? profile, string guiPath, string? profileFile, Dictionary<string, string> overrides)
    {
        JObject overlay = new();
        if (profileFile is not null)
        {
            var profiles = (JObject?)Read(profileFile)["profiles"] ?? throw new ArgumentException("Missing profiles object.");
            overlay = (JObject?)profiles[profile ?? "pbz"] ?? throw new ArgumentException("Unknown CLI profile.");
            var baseDirectory = Path.GetDirectoryName(Path.GetFullPath(profileFile))!;
            foreach (var field in new[] { "guiSettings", "input", "gameDirectory", "mapping", "dataDirectory", "keysFile" })
                if (overlay.Value<string>(field) is { Length: > 0 } value)
                    overlay[field] = Path.GetFullPath(Path.Combine(baseDirectory, Environment.ExpandEnvironmentVariables(value)));
            guiPath = overlay.Value<string>("guiSettings") ?? guiPath;
        }
        foreach (var pair in overrides) overlay[pair.Key] = pair.Value;
        var root = File.Exists(guiPath) ? Read(guiPath) : new JObject();
        var per = (JObject?)root["PerDirectory"] ?? new JObject();
        var input = overlay.Value<string>("input") ?? overlay.Value<string>("gameDirectory");
        JObject? dir = null;
        if (input is not null) dir = (JObject?)per[input];
        else if (profile is not null)
        {
            var match = per.Properties().Where(p => string.Equals(p.Value.Value<string>("GameName"), profile, StringComparison.OrdinalIgnoreCase) ||
                string.Equals(p.Name, profile, StringComparison.OrdinalIgnoreCase) ||
                (profile.Equals("pbz", StringComparison.OrdinalIgnoreCase) && p.Name.Contains("PBZ", StringComparison.OrdinalIgnoreCase))).ToArray();
            if (match.Length != 1) throw new ArgumentException("Profile must match exactly one configured local directory; use --input or --profile-file.");
            input = match[0].Name;
            dir = (JObject)match[0].Value;
        }
        else
        {
            input = root.Value<string>("GameDirectory");
            if (input is not null) dir = (JObject?)per[input];
        }
        if (input is null) throw new ArgumentException("Missing local input directory; configure FModel or specify --input.");
        dir ??= new JObject();
        var config = FromGui(root, dir, input);
        var keys = new Dictionary<string, string>(config.Keys, StringComparer.OrdinalIgnoreCase);
        if (overlay.Value<string>("keysFile") is { } keysPath)
            foreach (var pair in Read(keysPath).Properties()) keys[pair.Name] = pair.Value.Value<string>()!;
        if (Environment.GetEnvironmentVariable("FMODEL_AES_KEY") is { Length: > 0 } key) keys[new string('0', 32)] = key;
        return config with
        {
            Mapping = overlay.Value<string>("mapping") ?? config.Mapping,
            DataDirectory = overlay.Value<string>("dataDirectory") ?? config.DataDirectory,
            Game = overlay["game"] is { } game ? ParseEnum<EGame>(game) : config.Game,
            Platform = overlay["platform"] is { } platform ? ParseEnum<ETexturePlatform>(platform) : config.Platform,
            ReadScriptData = overlay["readScriptData"]?.Value<bool>() ?? config.ReadScriptData,
            ReadShaderMaps = overlay["readShaderMaps"]?.Value<bool>() ?? config.ReadShaderMaps,
            HashInputs = overlay["hashInputs"]?.Value<bool>() ?? false,
            Keys = keys
        };
    }
    public static SessionConfig FromGui(JObject root, JObject dir, string? input = null)
    {
        var keys = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        var aes = dir["AesKeys"];
        var main = aes?.Value<string>("mainKey");
        if (!string.IsNullOrEmpty(main)) keys[new string('0', 32)] = main;
        foreach (var dynamicKey in aes?["dynamicKeys"] ?? new JArray())
            if (dynamicKey.Value<string>("guid") is { } guid && dynamicKey.Value<string>("key") is { } key) keys[guid] = key;
        var mappings = dir["Endpoints"]?.Skip(1).FirstOrDefault();
        var versioning = dir["Versioning"];
        return new SessionConfig
        {
            InputDirectory = input ?? dir.Value<string>("GameDirectory") ?? root.Value<string>("GameDirectory") ?? "",
            Mapping = mappings?.Value<string>("FilePath") ?? "",
            DataDirectory = Path.Combine(root.Value<string>("OutputDirectory") ?? AppContext.BaseDirectory, ".data"),
            Game = dir["UeVersion"] is { } g ? ParseEnum<EGame>(g) : EGame.GAME_UE5_LATEST,
            Platform = dir["TexturePlatform"] is { } p ? ParseEnum<ETexturePlatform>(p) : ETexturePlatform.DesktopMobile,
            ReadScriptData = root.Value<bool>("ReadScriptData"),
            ReadShaderMaps = root.Value<bool>("ReadShaderMaps"),
            Keys = keys,
            CustomVersions = versioning?["CustomVersions"]?.ToObject<FCustomVersion[]>() ?? [],
            VersionOptions = versioning?["Options"]?.ToObject<Dictionary<string, bool>>(),
            MapStructTypes = versioning?["MapStructTypes"]?.ToObject<Dictionary<string, KeyValuePair<string, string>>>()
        };
    }
    private static T ParseEnum<T>(JToken value) where T : struct, Enum => Enum.Parse<T>(value.ToString(), true);
}

public static partial class SafeText
{
    [GeneratedRegex("(?:0x)?[0-9a-fA-F]{64}")]
    private static partial Regex KeyPattern();
    public static string Redact(string text) => KeyPattern().Replace(text, "[redacted-key]");
    public static string Hash(string text) => Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(text)));
    public static string FileHash(string path)
    {
        using var file = File.OpenRead(path);
        return Convert.ToHexString(SHA256.HashData(file));
    }
}
