using System.Globalization;
using System.IO;
using System.Security.Cryptography;
using System.Text;
using FModel.Export;
using Newtonsoft.Json;

internal static class ParallelStress
{
    public static async Task<int> Run(string[] args)
    {
        var samplePath = args[Array.IndexOf(args, "--samples") + 1];
        var paths = File.ReadAllLines(samplePath);
        var baseline = Path.Combine(Path.GetDirectoryName(samplePath)!, "old-ui-chain");
        var expected = paths.ToDictionary(p => p, p => Hash(OutputSafety.Target(baseline, p, true)));
        var root = Path.Combine("I:/PBZ/Analysis/ExportStress", DateTime.Now.ToString("yyyyMMdd-HHmmss"));
        Directory.CreateDirectory(root);
        var factory = new CueSessionFactory(ConfigLoader.Load("pbz", ConfigLoader.DefaultGuiPath, null, []));
        var runs = new List<object>();
        for (var run = 1; run <= 3; run++)
        {
            var output = Path.Combine(root, "run-" + run);
            var report = await new ExportRunner().RunAsync(new Probe(factory), new ExportOptions { Output = output, Workers = 4, MemoryMb = 4096 }, null, true, paths);
            foreach (var path in paths)
                if (Hash(OutputSafety.Target(output, path, true)) != expected[path]) throw new InvalidOperationException("Concurrent semantic mismatch: " + path);
            if (report.Counts.GetValueOrDefault("failed") != 0 || report.Unprocessed != 0 || report.ActualWorkers != 4)
                throw new InvalidOperationException("Concurrent run did not complete as expected.");
            runs.Add(new { run, report.ActualWorkers, report.ExitCode, report.Counts, report.ExportMilliseconds, report.PeakMemoryBytes, semanticMatches = paths.Length });
            Console.WriteLine($"Stress {run}: {paths.Length} semantic matches; workers={report.ActualWorkers}; {report.ExportMilliseconds / 1000:F2}s");
            GC.Collect(2, GCCollectionMode.Forced, true, true);
        }
        File.WriteAllText(Path.Combine(root, "stress-results.json"), WireJson.Serialize(new { runs, samplePath, root }));
        Console.WriteLine(root);
        return 0;
    }
    private static string Hash(string path)
    {
        using var file = File.OpenText(path);
        using var reader = new JsonTextReader(file) { DateParseHandling = DateParseHandling.None };
        using var hash = IncrementalHash.CreateHash(HashAlgorithmName.SHA256);
        while (reader.Read())
        {
            var value = Convert.ToString(reader.Value, CultureInfo.InvariantCulture) ?? "";
            hash.AppendData(Encoding.UTF8.GetBytes($"{(int)reader.TokenType}:{value.Length}:{value}\n"));
        }
        return Convert.ToHexString(hash.GetHashAndReset());
    }
    private sealed class Probe(CueSessionFactory factory) : ISessionFactory
    {
        public string InputDirectory => factory.InputDirectory;
        public bool ParallelSafe => true;
        public IExportSession Create() => factory.Create();
    }
}
