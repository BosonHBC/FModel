using System.Diagnostics;
using System.Text;
using FModel.Export;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;
using Serilog;

Log.Logger = new LoggerConfiguration().WriteTo.Sink(DiagnosticSink.Instance).CreateLogger();
var root = Path.Combine("I:/PBZ/Analysis/ExportTests", DateTime.Now.ToString("yyyyMMdd-HHmmss") + "-" + Guid.NewGuid().ToString("N")[..6]);
Directory.CreateDirectory(root);
var checks = new List<string>();
void Check(bool value, string name) { if (!value) throw new InvalidOperationException("FAILED: " + name); checks.Add(name); Console.WriteLine("PASS: " + name); }
var factory = new FakeFactory(Path.Combine(root, "input"));
Directory.CreateDirectory(factory.InputDirectory);
var output = Path.Combine(root, "output");
var options = new ExportOptions { Output = output, Workers = 2, HashOutputs = true };
var events = new List<ExportEvent>();
var runner = new ExportRunner(e => { lock (events) events.Add(e); });
var report = await runner.RunAsync(factory, options, null, true, null);
Check(report.ExitCode == 0 && report.Results.All(r => r.State == "success") && factory.Loads == 4, "full export with isolated concurrent workers");
Check(factory.MaxActive >= 2, "actual worker overlap");
Check(report.ActualWorkers == 2 && report.Unprocessed == 0, "report counts");
Check(JArray.Parse(File.ReadAllText(OutputSafety.Target(output, "Game/Content/a.uasset", true))).Count == 6001, "all exports including paginated-size package");
Check(events.First().Phase == "started" && events.Last().Phase == "finished", "structured lifecycle");
var priorLoads = factory.Loads;
report = await runner.RunAsync(factory, options, null, true, null);
Check(report.Results.All(r => r.State == "skipped") && factory.Loads == priorLoads, "skip happens before package parsing");
report = await runner.RunAsync(factory, options with { Existing = "resume" }, null, true, null);
Check(report.Results.All(r => r.State == "skipped") && factory.Loads == priorLoads, "resume verified records");
var fileA = OutputSafety.Target(output, "Game/Content/a.uasset", true);
File.WriteAllText(fileA, "broken");
report = await runner.RunAsync(factory, options with { VerifyExisting = true }, null, true, null);
Check(report.Counts.GetValueOrDefault("success") == 1 && factory.Loads == priorLoads + 1, "corrupt output reparsed");
File.WriteAllText(fileA, "");
report = await runner.RunAsync(factory, options, null, true, null);
Check(report.Counts.GetValueOrDefault("success") == 1, "zero-byte output reparsed");
factory.FingerprintValue = "changed-input-usmap";
report = await runner.RunAsync(factory, options with { Existing = "resume" }, null, true, null);
Check(report.Counts.GetValueOrDefault("success") == 4, "source/usmap fingerprint change reparses");
var compactOutput = Path.Combine(root, "compact");
report = await runner.RunAsync(factory, options with { Output = compactOutput, Compact = true }, null, true, null);
Check(JToken.DeepEquals(JToken.Parse(File.ReadAllText(fileA)), JToken.Parse(File.ReadAllText(OutputSafety.Target(compactOutput, "Game/Content/a.uasset", true)))), "compact JSON semantic equivalence");
var legacyOutput = Path.Combine(root, "legacy");
Directory.CreateDirectory(Path.GetDirectoryName(OutputSafety.Target(legacyOutput, "Game/Content/a.uasset", true))!);
File.WriteAllText(OutputSafety.Target(legacyOutput, "Game/Content/a.uasset", true), "[]");
report = await runner.RunAsync(factory, options with { Output = legacyOutput, HashOutputs = false }, null, true, ["Game/Content/a.uasset"]);
Check(report.Results.Single().State == "skipped-unverified", "legacy skip clearly unverified");
factory.Fail = "Game/Content/b.umap";
report = await runner.RunAsync(factory, options with { Existing = "overwrite" }, null, true, null);
Check(report.ExitCode == 2 && report.Counts.GetValueOrDefault("failed") == 1, "ordinary failures continue and exit 2");
Check(File.Exists(OutputSafety.Target(output, factory.Fail, true)), "old complete JSON retained on parse exception");
factory.Fail = null;
factory.Partial = "Game/Content/c.umap";
report = await runner.RunAsync(factory, options with { Existing = "overwrite" }, null, true, null);
Check(report.ExitCode == 2 && report.Counts.GetValueOrDefault("partial") == 1, "logged parser errors classified partial");
factory.Partial = null;
priorLoads = factory.Loads;
report = await runner.RunAsync(factory, options, null, true, null);
Check(factory.Loads == priorLoads + 1 && report.Counts.GetValueOrDefault("success") == 1, "known partial output is not skipped");
using (var held = OutputSafety.AcquireLock(output))
{
    try { await runner.RunAsync(factory, options, null, true, null); Check(false, "output lock"); }
    catch (IOException) { Check(true, "output lock prevents competing writers"); }
}
try { OutputSafety.Target(output, "../escape.uasset", true); Check(false, "traversal"); }
catch (ArgumentException) { Check(true, "path traversal rejected"); }
try { OutputSafety.Validate(Path.Combine(factory.InputDirectory, "output"), factory.InputDirectory); Check(false, "game output"); }
catch (ArgumentException) { Check(true, "game directory writes rejected"); }
factory.Collision = true;
try { await runner.RunAsync(factory, options with { KeepStructure = false }, null, true, null); Check(false, "collision"); }
catch (ArgumentException) { Check(true, "flat collisions rejected"); }
factory.Collision = false;
var atomic = Path.Combine(root, "atomic.json");
File.WriteAllText(atomic, "old");
try { OutputSafety.AtomicWrite(atomic, w => { w.Write("partial"); throw new IOException("Injected full disk"); }); }
catch (IOException) { }
Check(File.ReadAllText(atomic) == "old" && !Directory.GetFiles(root, "*.tmp").Any(), "atomic failure preserves old output and cleans temp");
factory.Fatal = true;
report = await runner.RunAsync(factory, options with { Existing = "overwrite" }, null, true, null);
Check(report.ExitCode == 4, "injected disk failure gives task exit 4");
factory.Fatal = false;
var cancelledOutput = Path.Combine(root, "cancelled");
using (var cancel = new CancellationTokenSource(120))
{
    report = await runner.RunAsync(factory, options with { Output = cancelledOutput }, null, true, null, cancel.Token);
    Check(report.ExitCode == 3, "cooperative cancellation exit 3 and report saved");
}
report = await runner.RunAsync(factory, options with { Output = cancelledOutput, Existing = "resume" }, null, true, null);
Check(report.ExitCode == 0 && report.Unprocessed == 0, "cancelled task resumes");
factory.ParallelSafeValue = false;
report = await runner.RunAsync(factory, options, null, true, null);
Check(report.ActualWorkers == 1 && report.Diagnostics.Any(), "unverified presets safely fall back");
Check(SafeText.Redact("0x" + new string('A', 64)) == "[redacted-key]", "secret redaction");
var manifestPath = Path.Combine(root, "tail.jsonl");
File.WriteAllText(manifestPath, WireJson.Serialize(report.Results.First()) + "\n{broken");
using (var manifest = new ManifestStore(manifestPath)) manifest.Append(report.Results.First());
using (var manifest = new ManifestStore(manifestPath)) Check(manifest.Records.Count == 1, "interrupted manifest tail recovers across repeated startup");
File.AppendAllText(manifestPath, "{broken\n");
using (var manifest = new ManifestStore(manifestPath)) manifest.Append(report.Results.First());
using (var manifest = new ManifestStore(manifestPath)) Check(manifest.Records.Count == 1, "malformed final manifest line with newline also recovers");
var assemblies = typeof(ExportRunner).Assembly.GetReferencedAssemblies().Select(a => a.Name ?? "").ToArray();
Check(!assemblies.Any(a => a.Contains("Presentation") || a.Contains("Snooper") || a == "FModel"), "core has no GUI/renderer assembly reference");
ConfigurationChecks.Run(root, Check);
if (args.Contains("--cli")) await ProcessChecks.Run(args[Array.IndexOf(args, "--cli") + 1], root, Check);
File.WriteAllText(Path.Combine(root, "test-results.json"), WireJson.Serialize(new { passed = checks.Count, checks, root }));
Console.WriteLine($"{checks.Count} checks passed. Evidence: {root}");
return 0;

sealed class FakeFactory(string directory) : ISessionFactory
{
    public string InputDirectory => directory;
    public bool ParallelSafe => ParallelSafeValue;
    public bool ParallelSafeValue = true;
    public string FingerprintValue = "initial";
    public int Loads;
    public int Active;
    public int MaxActive;
    public string? Fail;
    public string? Partial;
    public bool Fatal;
    public bool Collision;
    public IExportSession Create() => new FakeSession(this);
}
sealed class FakeSession(FakeFactory factory) : IExportSession
{
    public string Fingerprint => factory.FingerprintValue;
    public IReadOnlyList<PackageItem> List(string? path, bool recursive, IEnumerable<string>? files = null)
    {
        var items = new[] { "Game/Content/a.uasset", "Game/Content/b.umap", "Game/Content/c.umap", "Game/Content/d.uasset" };
        if (factory.Collision) items = ["Game/Content/a.uasset", "Game/Other/a.uasset"];
        return (files ?? items).Select(p => new PackageItem(p, 128)).ToArray();
    }
    public void WriteJson(string path, TextWriter writer, bool compact, CancellationToken token)
    {
        Interlocked.Increment(ref factory.Loads);
        var active = Interlocked.Increment(ref factory.Active);
        lock (factory) factory.MaxActive = Math.Max(factory.MaxActive, active);
        try
        {
            Thread.Sleep(150);
            token.ThrowIfCancellationRequested();
            if (factory.Fatal) throw new IOException("Injected disk failure");
            if (path == factory.Fail) throw new InvalidDataException("Injected parsing failure");
            if (path == factory.Partial) Log.Error("Injected missing mapping for ReallinkPerfCamera");
            using var json = new JsonTextWriter(writer) { Formatting = compact ? Formatting.None : Formatting.Indented, CloseOutput = false };
            JsonSerializer.CreateDefault().Serialize(json, Enumerable.Range(0, 6001).Select(i => new { Type = "Test", Name = i.ToString(), Path = path }));
            json.Flush();
        }
        finally { Interlocked.Decrement(ref factory.Active); }
    }
    public string PackageFingerprint(PackageItem item) => SafeText.Hash(Fingerprint + item.Path);
    public void ClearCache() { }
    public void Dispose() { }
}
