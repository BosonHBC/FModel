using FModel.Export;
using CUE4Parse.UE4.Versions;
using Newtonsoft.Json.Linq;

internal static class ConfigurationChecks
{
    public static void Run(string root, Action<bool, string> check)
    {
        var gui = Path.Combine(root, "gui.json");
        var input = Path.Combine(root, "PBZ input with spaces");
        var settings = new JObject
        {
            ["GameDirectory"] = input, ["OutputDirectory"] = Path.Combine(root, "gui-output"),
            ["ReadShaderMaps"] = true,
            ["PerDirectory"] = new JObject
            {
                [input] = new JObject
                {
                    ["GameName"] = "PBZ", ["GameDirectory"] = input, ["UeVersion"] = (int)EGame.GAME_PhantomBladeZero,
                    ["TexturePlatform"] = 0, ["Versioning"] = new JObject(),
                    ["AesKeys"] = new JObject { ["mainKey"] = "0x" + new string('1', 64) },
                    ["Endpoints"] = new JArray(new JObject(), new JObject { ["FilePath"] = Path.Combine(root, "gui.usmap") })
                }
            }
        };
        File.WriteAllText(gui, settings.ToString());
        var before = SafeText.FileHash(gui);
        var profile = Path.Combine(root, "profiles.json");
        File.WriteAllText(profile, new JObject { ["profiles"] = new JObject { ["pbz"] = new JObject { ["mapping"] = "profile mapping.usmap", ["dataDirectory"] = ".data" } } }.ToString());
        var config = ConfigLoader.Load("pbz", gui, profile, new Dictionary<string, string> { ["mapping"] = Path.Combine(root, "override mapping.usmap") });
        check(config.Mapping == Path.Combine(root, "override mapping.usmap") && config.ReadShaderMaps, "explicit > profile > GUI configuration precedence");
        check(config.DataDirectory == Path.GetFullPath(Path.Combine(root, ".data")), "profile relative paths resolve against profile file");
        check(config.Game == EGame.GAME_PhantomBladeZero && config.Keys.Count == 1, "GUI numeric game version and AES are imported");
        check(!WireJson.Serialize(config).Contains(new string('1', 64)), "configuration serialization excludes AES");
        check(SafeText.FileHash(gui) == before, "CLI configuration import does not modify GUI settings");
        try { ConfigLoader.Load("unknown", gui, null, []); check(false, "unknown profile"); }
        catch (ArgumentException) { check(true, "unknown profile rejected"); }
        var cli = ConfigLoader.Load("pbz", gui, null, new Dictionary<string, string> { ["readShaderMaps"] = "false" });
        check(!cli.ReadShaderMaps, "explicit false parsing override");
        settings["GameDirectory"] = Path.Combine(root, "other game");
        File.WriteAllText(gui, settings.ToString());
        var sticky = ConfigLoader.Load("pbz", gui, profile, []);
        check(sticky.InputDirectory == input, "named CLI profile remains bound to PBZ after GUI game selection changes");
    }
}
