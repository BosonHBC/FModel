using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Security.Cryptography;
using System.Text;
using System.Windows.Threading;
using CUE4Parse.Encryption.Aes;
using CUE4Parse.FileProvider;
using CUE4Parse.MappingsProvider.Usmap;
using CUE4Parse.UE4.Objects.Core.Misc;
using FModel.Export;
using ICSharpCode.AvalonEdit.Document;
using Newtonsoft.Json;
using Serilog;

Log.Logger = new LoggerConfiguration().WriteTo.Sink(DiagnosticSink.Instance).CreateLogger();
if (args.Contains("--stress")) return await ParallelStress.Run(args);
var root = Path.Combine("I:/PBZ/Analysis/ExportBenchmark", DateTime.Now.ToString("yyyyMMdd-HHmmss"));
Directory.CreateDirectory(root);
var config = ConfigLoader.Load("pbz", ConfigLoader.DefaultGuiPath, null, []);
var factory = new CueSessionFactory(config);
string[] paths;
using (var session = factory.Create())
{
    var items = session.List("ProjectPBZ/Content/Maps/B08_09_10", true);
    var cells = items.Where(f => f.Path.Contains("/_Generated_/", StringComparison.OrdinalIgnoreCase)).ToArray();
    paths = Enumerable.Range(0, 64).Select(i => cells[i * (cells.Length - 1) / 63].Path)
        .Concat(items.Where(f => f.Path.EndsWith("PBZ_Pangzhen_WP.umap", StringComparison.OrdinalIgnoreCase) || f.Path.EndsWith("_BuiltData.uasset", StringComparison.OrdinalIgnoreCase)).Select(f => f.Path))
        .Concat(File.ReadLines("I:/PBZ/Analysis/Data/pangzhen-direct-imports.txt").Where(f => f.Contains("PBZContent/Environments") && f.EndsWith(".uasset")).Take(6))
        .Distinct(StringComparer.OrdinalIgnoreCase).ToArray();
}
File.WriteAllLines(Path.Combine(root, "samples.txt"), paths);
Console.WriteLine($"Benchmark: {paths.Length} packages including Pangzhen root; evidence: {root}");
var baseline = Path.Combine(root, "old-ui-chain");
var peak = 0L;
var stop = new CancellationTokenSource();
var monitor = Task.Run(async () => { while (!stop.IsCancellationRequested) { peak = Math.Max(peak, Process.GetCurrentProcess().WorkingSet64); await Task.Delay(100); } });
Dispatcher? dispatcher = null;
var ready = new ManualResetEventSlim();
var uiThread = new Thread(() => { dispatcher = Dispatcher.CurrentDispatcher; ready.Set(); Dispatcher.Run(); });
uiThread.SetApartmentState(ApartmentState.STA);
uiThread.Start();
ready.Wait();
var oldTimer = Stopwatch.StartNew();
using (var provider = new DefaultFileProvider(config.InputDirectory, SearchOption.AllDirectories, config.Versions(), StringComparer.OrdinalIgnoreCase)
    { ReadScriptData = config.ReadScriptData, ReadShaderMaps = config.ReadShaderMaps, ReadNaniteData = true, MappingsContainer = new FileUsmapTypeMappingsProvider(config.Mapping) })
{
    provider.Initialize();
    foreach (var key in config.Keys) provider.SubmitKey(new FGuid(key.Key), new FAesKey(key.Value));
    provider.PostMount();
    var init = oldTimer.Elapsed.TotalMilliseconds;
    TextDocument? document = null;
    foreach (var path in paths)
    {
        var text = JsonConvert.SerializeObject(provider.LoadPackage(path).GetExports(), Formatting.Indented);
        var target = OutputSafety.Target(baseline, path, true);
        dispatcher!.Invoke(() =>
        {
            document ??= new TextDocument();
            document.Text = text;
            document.UndoStack.ClearAll();
            Directory.CreateDirectory(Path.GetDirectoryName(target)!);
            File.WriteAllText(target, document.Text);
        });
    }
    File.WriteAllText(Path.Combine(root, "old-timing.json"), WireJson.Serialize(new { initializationMilliseconds = init, exportMilliseconds = oldTimer.Elapsed.TotalMilliseconds - init }));
}
var oldTotal = oldTimer.Elapsed.TotalMilliseconds;
stop.Cancel();
await monitor;
dispatcher!.BeginInvokeShutdown(DispatcherPriority.Normal);
uiThread.Join();
Console.WriteLine($"Old serial UI-document chain: {oldTotal / 1000:F2}s; peak {peak / 1048576:F0} MiB");
GC.Collect(2, GCCollectionMode.Forced, true, true);
var runs = new List<object>();
var oldHashes = paths.ToDictionary(p => p, p => Semantic(OutputSafety.Target(baseline, p, true)));
foreach (var workers in new[] { 1, 2 })
{
    var output = Path.Combine(root, "new-" + workers);
    var report = await new ExportRunner(e => { if (e.Phase == "finished") Console.WriteLine($"New {workers}: {e.Processed}/{e.Total} done"); })
        .RunAsync(new ParallelProbe(factory), new ExportOptions { Output = output, Workers = workers, MemoryMb = 8192 }, null, true, paths);
    foreach (var p in paths)
    {
        var hash = Semantic(OutputSafety.Target(output, p, true));
        if (hash.Hash != oldHashes[p].Hash) throw new InvalidOperationException("Semantic mismatch: " + p);
    }
    runs.Add(new { requestedWorkers = workers, report.ActualWorkers, report.InitializeMilliseconds, report.ExportMilliseconds, report.PeakMemoryBytes, report.ExitCode, report.Counts,
        semanticMatches = paths.Length, folder = output });
    Console.WriteLine($"New {workers}: export {report.ExportMilliseconds / 1000:F2}s; init {report.InitializeMilliseconds / 1000:F2}s; peak {report.PeakMemoryBytes / 1048576:F0} MiB; semantics match");
    GC.Collect(2, GCCollectionMode.Forced, true, true);
}
var newOne = Path.Combine(root, "new-1");
var skip = await new ExportRunner().RunAsync(factory, new ExportOptions { Output = newOne, Existing = "skip", Workers = 1 }, null, true, paths);
var compactFiles = paths.Where(p => !p.EndsWith("PBZ_Pangzhen_WP.umap")).Take(8).ToArray();
var compactDir = Path.Combine(root, "compact");
var compact = await new ExportRunner().RunAsync(factory, new ExportOptions { Output = compactDir, Compact = true, Workers = 1 }, null, true, compactFiles);
foreach (var p in compactFiles)
    if (Semantic(OutputSafety.Target(compactDir, p, true)).Hash != oldHashes[p].Hash) throw new InvalidOperationException("Compact mismatch: " + p);
var existingMatches = new List<string>();
foreach (var p in paths)
{
    var target = OutputSafety.Target("I:/PBZ/FModelOutputs/Exports", p, true);
    if (File.Exists(target) && Semantic(target).Hash == oldHashes[p].Hash) existingMatches.Add(p);
}
var summary = new
{
    root, sampleCount = paths.Length, oldChainTotalMilliseconds = oldTotal, oldPeakMemoryBytes = peak,
    cacheConditions = "All runs in one benchmark process; disk cache warm and order old, new-1, new-2. Old chain reproduces complete serialization, Dispatcher and AvalonEdit document/write; not full interactive app startup.",
    runs, skip.ExportMilliseconds, skip.Counts, compactMatches = compactFiles.Length, existingMatches = existingMatches.Count,
    rootExports = oldHashes[paths.Single(p => p.EndsWith("PBZ_Pangzhen_WP.umap"))].Exports,
    fastGeoSampleCount = oldHashes.Values.Count(v => v.FastGeo), hlodSampleCount = oldHashes.Values.Count(v => v.Hlod),
    sampleDetails = paths.Select(p => new { path = p, oldHashes[p].Exports, oldHashes[p].FastGeo, oldHashes[p].Hlod })
};
File.WriteAllText(Path.Combine(root, "benchmark-results.json"), WireJson.Serialize(summary));
Console.WriteLine(WireJson.Serialize(summary));
return 0;

static SemanticResult Semantic(string path)
{
    using var file = File.OpenText(path);
    using var reader = new JsonTextReader(file) { DateParseHandling = DateParseHandling.None };
    using var hash = IncrementalHash.CreateHash(HashAlgorithmName.SHA256);
    var exports = 0;
    var fastGeo = false;
    var hlod = false;
    while (reader.Read())
    {
        if (reader.TokenType == JsonToken.StartObject && reader.Depth == 1) exports++;
        var value = Convert.ToString(reader.Value, CultureInfo.InvariantCulture) ?? "";
        fastGeo |= value.Contains("FastGeoContainer", StringComparison.Ordinal);
        hlod |= value.Contains("HLOD", StringComparison.OrdinalIgnoreCase);
        var bytes = Encoding.UTF8.GetBytes($"{(int)reader.TokenType}:{value.Length}:{value}\n");
        hash.AppendData(bytes);
    }
    return new SemanticResult(Convert.ToHexString(hash.GetHashAndReset()), exports, fastGeo, hlod);
}
sealed record SemanticResult(string Hash, int Exports, bool FastGeo, bool Hlod);
sealed class ParallelProbe(CueSessionFactory factory) : ISessionFactory
{
    public string InputDirectory => factory.InputDirectory;
    public bool ParallelSafe => true;
    public IExportSession Create() => factory.Create();
}
