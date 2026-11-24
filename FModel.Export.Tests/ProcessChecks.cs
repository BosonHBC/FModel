using System.Diagnostics;
using FModel.Export;
using Newtonsoft.Json.Linq;

internal static class ProcessChecks
{
    public static async Task Run(string binary, string root, Action<bool, string> check)
    {
        var actualSettingsHash = SafeText.FileHash(ConfigLoader.DefaultGuiPath);
        async Task<(int Exit, string Out, string Err)> Execute(params string[] args)
        {
            var info = new ProcessStartInfo { FileName = binary.EndsWith(".dll") ? "dotnet" : binary, UseShellExecute = false, RedirectStandardOutput = true, RedirectStandardError = true, CreateNoWindow = true };
            if (binary.EndsWith(".dll")) info.ArgumentList.Add(binary);
            foreach (var arg in args) info.ArgumentList.Add(arg);
            using var process = Process.Start(info)!;
            var stdout = process.StandardOutput.ReadToEndAsync();
            var stderr = process.StandardError.ReadToEndAsync();
            await process.WaitForExitAsync();
            return (process.ExitCode, await stdout, await stderr);
        }
        var result = await Execute("--help", "--mapping", "does-not-exist.usmap");
        check(result.Exit == 0 && result.Out.Contains("export-json"), "CLI help needs no game initialization");
        result = await Execute("--version");
        check(result.Exit == 0 && result.Out.Contains("json-export"), "CLI version contract");
        result = await Execute("export-json", "--unexpected");
        check(result.Exit == 1, "CLI bad arguments exit 1");
        result = await Execute("profiles", "list", "--format", "json");
        check(result.Exit == 0 && JArray.Parse(result.Out).Count > 0, "CLI profile enumeration is valid JSON");
        result = await Execute("list", "--profile", "pbz", "--path", "ProjectPBZ/Content/Maps/B08_09_10", "--format", "paths");
        check(result.Exit == 0 && result.Out.Contains("PBZ_Pangzhen_WP.umap") && !result.Out.Contains("Warning"), "CLI list paths with diagnostics confined to stderr");
        var output = Path.Combine(root, "cli-output");
        var selected = Path.Combine(root, "cli-selected.txt");
        File.WriteAllText(selected, "ProjectPBZ/Content/Maps/B08_09_10/PBZ_Pangzhen_WP_BuiltData.uasset\n");
        result = await Execute("export-json", "--profile", "pbz", "--files-from", selected, "--output", output, "--events", "jsonl");
        var lines = result.Out.Split('\n', StringSplitOptions.RemoveEmptyEntries).Select(JObject.Parse).ToArray();
        check(result.Exit == 0 && lines.Last().Value<int>("exitCode") == 0 && lines.First().Value<string>("phase") == "started", "CLI JSONL export and summary contract");
        result = await Execute("export-json", "--profile", "pbz", "--files-from", selected, "--output", output, "--events", "jsonl");
        lines = result.Out.Split('\n', StringSplitOptions.RemoveEmptyEntries).Select(JObject.Parse).ToArray();
        check(result.Exit == 0 && lines.Last()["counts"]?.Value<int>("skipped") == 1, "CLI repeat export skips successful output");
        var partialOutput = Path.Combine(root, "cli-partial");
        File.WriteAllText(selected, "ProjectPBZ/Content/Maps/B08_09_10/PBZ_Pangzhen_WP.umap\n");
        result = await Execute("export-json", "--profile", "pbz", "--files-from", selected, "--output", partialOutput, "--events", "jsonl");
        lines = result.Out.Split('\n', StringSplitOptions.RemoveEmptyEntries).Select(JObject.Parse).ToArray();
        check(result.Exit == 2 && lines.Last()["counts"]?.Value<int>("partial") == 1, "CLI actual missing mapping produces partial and exit 2");
        var report = Path.Combine(partialOutput, ".fmodel-export", "latest-report.json");
        result = await Execute("export-json", "--profile", "pbz", "--retry-report", report, "--output", partialOutput, "--events", "jsonl");
        check(result.Exit == 2 && ConfigLoader.Read(report).Value<int>("total") == 1, "CLI retry report selects only the partial package");
        result = await Execute("export-json", "--profile", "pbz", "--retry-report", report, "--output", output);
        check(result.Exit == 1, "CLI incompatible retry output rejected");
        var cancel = Path.Combine(root, "cancel.request");
        File.WriteAllText(cancel, "cancel");
        result = await Execute("export-json", "--profile", "pbz", "--files-from", selected, "--output", Path.Combine(root, "cli-cancel"), "--cancel-file", cancel, "--events", "jsonl");
        check(result.Exit == 3, "CLI file-based cancellation exit 3");
        check(SafeText.FileHash(ConfigLoader.DefaultGuiPath) == actualSettingsHash, "all CLI processes leave real GUI settings untouched");
    }
}
