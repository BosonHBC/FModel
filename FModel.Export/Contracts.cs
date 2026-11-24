using Newtonsoft.Json;

namespace FModel.Export;

public sealed record ExportOptions
{
    public required string Output { get; init; }
    public string Existing { get; init; } = "skip";
    public int Workers { get; init; } = 2;
    public int MemoryMb { get; init; } = 4096;
    public bool Compact { get; init; }
    public bool KeepStructure { get; init; } = true;
    public bool VerifyExisting { get; init; }
    public bool HashOutputs { get; init; }
    public bool FailFast { get; init; }
    public string? Report { get; init; }
    public string? Manifest { get; init; }
    public string? CancelFile { get; init; }
}
public sealed record PackageItem(string Path, long Size);
public interface IExportSession : IDisposable
{
    IReadOnlyList<PackageItem> List(string? path, bool recursive, IEnumerable<string>? files = null);
    void WriteJson(string path, TextWriter writer, bool compact, CancellationToken token);
    string Fingerprint { get; }
    string PackageFingerprint(PackageItem item);
    void ClearCache();
}
public interface ISessionFactory
{
    IExportSession Create();
    string InputDirectory { get; }
    bool ParallelSafe { get; }
    int MaximumWorkers => ParallelSafe ? 32 : 1;
}
public sealed record PackageResult
{
    public required string Path { get; init; }
    public required string State { get; init; }
    public required string Fingerprint { get; init; }
    public long Length { get; init; }
    public string? Sha256 { get; init; }
    public double Milliseconds { get; init; }
    public string[] Diagnostics { get; init; } = [];
    public string? ErrorType { get; init; }
}
public sealed class ExportReport
{
    public const string CurrentVersion = "4.4.4-json-export-4-pbz-geometry";
    public int SchemaVersion { get; } = 1;
    public string ToolVersion { get; } = CurrentVersion;
    public string JobId { get; init; } = Guid.NewGuid().ToString("N");
    public required string Output { get; init; }
    public required string SessionFingerprint { get; init; }
    public required string Existing { get; init; }
    public int RequestedWorkers { get; init; }
    public int ActualWorkers { get; set; }
    public int Total { get; init; }
    public int Unprocessed => Total - Results.Count;
    public List<PackageResult> Results { get; } = [];
    public List<string> Diagnostics { get; } = [];
    public Dictionary<string, int> Counts => Results.GroupBy(x => x.State).ToDictionary(x => x.Key, x => x.Count());
    public double InitializeMilliseconds { get; set; }
    public double ExportMilliseconds { get; set; }
    public long PeakMemoryBytes { get; set; }
    public int ExitCode { get; set; }
}
public sealed record ExportEvent(int SchemaVersion, string JobId, string Phase, int Total, int Processed,
    Dictionary<string, int> Counts, int Workers, double ElapsedMilliseconds);
public static class WireJson
{
    public static readonly JsonSerializerSettings Settings = new()
    {
        ContractResolver = new Newtonsoft.Json.Serialization.CamelCasePropertyNamesContractResolver(),
        Formatting = Formatting.None
    };
    public static string Serialize(object value) => JsonConvert.SerializeObject(value, Settings);
}
