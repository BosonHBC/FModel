using System.Text;
using FModel.Export;
using Newtonsoft.Json.Linq;
using Serilog;

Console.OutputEncoding = new UTF8Encoding(false);
Log.Logger = new LoggerConfiguration().MinimumLevel.Information().WriteTo.Sink(DiagnosticSink.Instance).WriteTo.Sink(new RedactedConsoleSink()).CreateLogger();
try { return await MainAsync(args); }
catch (OperationCanceledException) { Console.Error.WriteLine("Cancelled before export started."); return 3; }
catch (Exception e) { Console.Error.WriteLine(SafeText.Redact(e.Message)); return 1; }
finally { Log.CloseAndFlush(); }

static async Task<int> MainAsync(string[] args)
{
    if (args.Length == 0 || args.Contains("--help") || args[0] == "help") { Console.WriteLine(CliHelp.Text); return 0; }
    if (args.Contains("--version") || args[0] == "version") { Console.WriteLine(ExportReport.CurrentVersion); return 0; }
    var command = args[0];
    var start = command == "profiles" ? 2 : 1;
    if (command == "profiles" && (args.Length < 2 || args[1] != "list")) throw new ArgumentException("Use profiles list.");
    if (command is not ("profiles" or "list" or "export-json" or "export-landscape" or "export-raw" or "export-geometry")) throw new ArgumentException("Unknown command; use --help.");
    var flags = new HashSet<string> { "recursive", "verify-existing", "hash-outputs", "hash-inputs", "fail-fast", "flat", "read-script-data", "read-shader-maps", "include-raw", "overwrite" };
    var accepted = new HashSet<string>(flags) { "profile", "profile-file", "gui-settings", "input", "mapping", "data-directory", "keys-file", "game", "platform", "path", "files-from", "output", "workers", "memory-mb", "existing", "format", "events", "report", "manifest", "retry-report", "cancel-file", "requests" };
    var values = new Dictionary<string, string>(StringComparer.Ordinal);
    for (var i = start; i < args.Length; i++)
    {
        if (!args[i].StartsWith("--")) throw new ArgumentException("Unexpected argument; use --help.");
        var name = args[i][2..];
        if (!accepted.Contains(name) || values.ContainsKey(name)) throw new ArgumentException("Unknown or repeated option: " + name);
        if (flags.Contains(name)) values[name] = "true";
        else if (i + 1 < args.Length && !args[i + 1].StartsWith("--")) values[name] = args[++i];
        else throw new ArgumentException("Missing option value: " + name);
    }
    string? Get(string key) => values.GetValueOrDefault(key);
    var resources = command is "export-landscape" or "export-raw";
    if (resources)
    {
        foreach (var flag in new[] { "retry-report", "manifest", "flat", "verify-existing", "hash-outputs", "fail-fast", "format", "workers", "memory-mb" })
            if (values.ContainsKey(flag)) throw new ArgumentException("Resource extraction does not support --" + flag + "; it runs one worker and verifies all output hashes.");
    }
    if (values.ContainsKey("include-raw") && command != "export-landscape") throw new ArgumentException("--include-raw is only valid for export-landscape.");
    var gui = Get("gui-settings") ?? ConfigLoader.DefaultGuiPath;
    if (command == "profiles")
    {
        if (Get("format") is not (null or "json")) throw new ArgumentException("profiles list supports --format json.");
        Console.WriteLine(WireJson.Serialize(ConfigLoader.Profiles(gui, Get("profile-file"))));
        return 0;
    }
    var overrides = new Dictionary<string, string>();
    foreach (var (flag, prop) in new[] { ("input", "input"), ("mapping", "mapping"), ("data-directory", "dataDirectory"), ("keys-file", "keysFile"), ("game", "game"), ("platform", "platform"), ("hash-inputs", "hashInputs"), ("read-script-data", "readScriptData"), ("read-shader-maps", "readShaderMaps") })
        if (Get(flag) is { } value) overrides[prop] = value;
    var config = ConfigLoader.Load(Get("profile"), gui, Get("profile-file"), overrides);
    var factory = new CueSessionFactory(config);
    using var cancellation = new CancellationTokenSource();
    Console.CancelKeyPress += (_, e) => { e.Cancel = true; cancellation.Cancel(); };
    IEnumerable<string>? files = Get("files-from") is { } list ? File.ReadAllLines(list) : null;
    if (Get("path") is not null && files is not null) throw new ArgumentException("Use --path OR --files-from.");
    if (command == "list")
    {
        var format = Get("format") ?? "json";
        if (format is not ("json" or "paths")) throw new ArgumentException("list format must be json or paths.");
        using var session = factory.Create();
        cancellation.Token.ThrowIfCancellationRequested();
        var packages = session.List(Get("path"), values.ContainsKey("recursive"), files);
        if (format == "paths") foreach (var package in packages) Console.WriteLine(package.Path);
        else Console.WriteLine(WireJson.Serialize(packages));
        return 0;
    }
    var output = Get("output") ?? throw new ArgumentException(command + " requires --output.");
    var events = Get("events") ?? "text";
    if (events is not ("text" or "jsonl")) throw new ArgumentException("events must be text or jsonl.");
    if (resources)
        return ResourceExtraction.Run(factory, command, output, Get("path"), values.ContainsKey("recursive"), files, Get("existing") ?? "skip", values.ContainsKey("include-raw"), Get("report"), Get("cancel-file"),
            e => { if (events == "jsonl") Console.WriteLine(WireJson.Serialize(e)); else Console.Error.WriteLine(WireJson.Serialize(e)); }, cancellation.Token);
    if (command == "export-geometry")
    {
        var requests = Get("requests") ?? throw new ArgumentException("export-geometry requires --requests FILE (JSON array of virtualPath/source/name).");
        if (files is not null || Get("path") is not null) throw new ArgumentException("export-geometry uses --requests, not --path/--files-from.");
        return GeometryExporter.Run(factory, requests, output, values.ContainsKey("overwrite"));
    }
    if (Get("retry-report") is { } retry)
    {
        if (files is not null || Get("path") is not null) throw new ArgumentException("retry-report cannot be combined with path/files-from.");
        var previous = ConfigLoader.Read(retry);
        if (!Path.GetFullPath(output).Equals(Path.GetFullPath(previous.Value<string>("output") ?? ""), StringComparison.OrdinalIgnoreCase))
            throw new ArgumentException("Retry output must match the previous report.");
        if (previous.Value<string>("sessionFingerprint") != factory.SessionFingerprint)
            throw new ArgumentException("Retry input/profile/mapping changed; use a new export with --existing resume instead.");
        files = previous["results"]?.Where(x => x.Value<string>("state") is "failed" or "partial").Select(x => x.Value<string>("path")!).ToArray() ?? [];
        if (!files.Any()) throw new ArgumentException("Report has no failed or partial packages to retry.");
    }
    var formatJson = Get("format") ?? "indented";
    if (formatJson is not ("indented" or "compact")) throw new ArgumentException("export-json format must be indented or compact.");
    var options = new ExportOptions
    {
        Output = output, Existing = Get("existing") ?? "skip", Workers = int.Parse(Get("workers") ?? "2"),
        MemoryMb = int.Parse(Get("memory-mb") ?? "4096"), Compact = formatJson == "compact", KeepStructure = !values.ContainsKey("flat"),
        VerifyExisting = values.ContainsKey("verify-existing"), HashOutputs = values.ContainsKey("hash-outputs"),
        FailFast = values.ContainsKey("fail-fast"), Report = Get("report"), Manifest = Get("manifest"), CancelFile = Get("cancel-file")
    };
    var runner = new ExportRunner(e =>
    {
        if (events == "jsonl") Console.WriteLine(WireJson.Serialize(e));
        else Console.Error.WriteLine($"{e.Phase}: {e.Processed}/{e.Total}, workers={e.Workers}, {e.ElapsedMilliseconds / 1000:F1}s");
    });
    var report = await runner.RunAsync(factory, options, Get("path"), values.ContainsKey("recursive"), files, cancellation.Token);
    var summary = new { schemaVersion = 1, phase = "summary", report.JobId, report.ExitCode, report.Counts, report.Unprocessed,
        report.ExportMilliseconds, report.PeakMemoryBytes, report.Diagnostics, reportPath = options.Report ?? Path.Combine(Path.GetFullPath(output), ".fmodel-export", "latest-report.json") };
    Console.WriteLine(WireJson.Serialize(summary));
    return report.ExitCode;
}

internal static class CliHelp
{
public const string Text = """
FModel.Cli: local property JSON and cooked Landscape extraction (.NET 10, Windows x64)

Commands:
  profiles list --format json [--gui-settings FILE] [--profile-file FILE]
  list --profile pbz --path ProjectPBZ/Content --recursive --format json|paths
  export-json --profile pbz --path ProjectPBZ/Content --recursive --output DIR
  export-json --profile pbz --files-from FILE --output DIR
  export-json --profile pbz --retry-report FILE --output SAME_DIR
  export-landscape --profile pbz --files-from FILE --output DIR [--include-raw]
  export-raw --profile pbz --files-from FILE --output DIR
  export-geometry --profile pbz --requests FILE --output DIR [--overwrite]
  --help | --version

Local configuration:
  --profile NAME --profile-file FILE --gui-settings FILE
  --input PAKS_DIR --game GAME_PhantomBladeZero --platform DesktopMobile
  --mapping FILE.usmap --data-directory DIR --keys-file FILE
  FMODEL_AES_KEY environment variable overrides the main key (never echoed).
  --hash-inputs hashes full container data (slow); default hashes utoc + metadata.
  --read-script-data --read-shader-maps enable optional parsing.

JSON export controls:
  --workers N (default 2, preset safety/memory may reduce it)
  --memory-mb N (default 4096, soft working-set budget, minimum 256)
  --existing skip|resume|overwrite (default skip)
  --format indented|compact (default indented) --flat
  --verify-existing --hash-outputs --fail-fast
  --events text|jsonl --report FILE --manifest FILE
  --cancel-file FILE (create this file to request cooperative cancellation)

Resource export controls:
  --path VIRTUAL_DIR --recursive OR --files-from FILE
  --existing skip|overwrite; skip validates input signature and all output hashes
  --events text|jsonl --report FILE --cancel-file FILE
  Single worker, package boundaries, periodic provider recycling.
  Landscape: mip0 .height.r16 (uint16 little-endian), per-layer .r8, all stored
  BGRA mip payloads, grass bytes, layer/channel mappings, original metadata.
  DesktopMobile non-VT BGRA only; unsupported formats fail, never downsample.
  PBZ edge-fixup snapshots preserved but NOT applied to derived heights.
  --include-raw preserves original package and associated Bulk/Optional payloads.
  export-raw needs no asset deserialization; outputs remain cooked IoStore data,
  not editor-importable uassets. No runtime RVT pages or original edit layers.

Geometry export controls (StaticMesh -> project geometry.json):
  --requests FILE: JSON array of {virtualPath, source, name}. virtualPath must be
  an original ProjectPBZ/Content package (no _Generated_/__ExternalActors__).
  --output DIR: each mesh writes {output}/{hash}/geometry.json (lods/materials/
  vertices/indices/sections), the same schema as PayloadExporter environment-evidence.
  Existing non-empty geometry.json is skipped (resume-friendly); --overwrite forces
  re-decode. A geometry-export-report.json records per-request state. Cooked Nanite
  clusters are decoded via StaticMeshDto; missing tangents are zero vectors.

Exit codes: 0 completed under selected skip policy; 1 setup/arguments failed;
2 completed with partial/failed packages; 3 cancelled; 4 runtime task failure.
Legacy nonempty JSON outputs skipped without records are skipped-unverified.
No automatic network access, key downloads, settings changes or game writes.
""";
}
