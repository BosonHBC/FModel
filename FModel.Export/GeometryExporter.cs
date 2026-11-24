using Newtonsoft.Json.Linq;

namespace FModel.Export;

/// <summary>
/// Batch decoder: cooked UStaticMesh -> geometry.json (project schema), driven by
/// an explicit request list so thousands of PBZ meshes can be decoded without the
/// 1..16 limit of PayloadExporter's environment-evidence command.
///
/// Requests: JSON array of { "virtualPath": "ProjectPBZ/Content/...uasset",
///   "source": "/Game/...Mesh.SM_X", "name": "SM_X" }.
/// Each result goes to {output}/{hash(virtualPath)}/geometry.json and a
/// batch report records state per request. Re-running skips existing successful
/// geometry.json outputs (resume-friendly, 240s client timeouts are safe).
/// </summary>
public static class GeometryExporter
{
    public static int Run(CueSessionFactory factory, string requestsPath, string output, bool overwrite = false)
    {
        OutputSafety.Validate(output, factory.InputDirectory);
        var requests = JArray.Parse(File.ReadAllText(requestsPath));
        if (requests.Count == 0) throw new ArgumentException("Empty geometry request list");
        var marker = Path.Combine(output, ".pbz-geometry-export");
        Directory.CreateDirectory(output);
        if (File.Exists(marker) && File.ReadAllText(marker) != "pbz-geometry-v1")
            throw new IOException("Refuse unowned geometry output directory");
        File.WriteAllText(marker, "pbz-geometry-v1");
        using var outputLock = OutputSafety.AcquireLock(output);
        using var session = (CueSession)factory.Create();
        var results = new List<object>();
        var failed = 0;
        foreach (var request in requests.OfType<JObject>())
        {
            var virtualPath = request.Value<string>("virtualPath")!;
            var source = request.Value<string>("source")!;
            var name = request.Value<string>("name")!;
            if (string.IsNullOrWhiteSpace(virtualPath) || string.IsNullOrWhiteSpace(source) || string.IsNullOrWhiteSpace(name))
                throw new ArgumentException("Request needs virtualPath, source and name");
            if (!virtualPath.StartsWith("ProjectPBZ/", StringComparison.OrdinalIgnoreCase)
                || virtualPath.Split('/').Any(x => x is "_Generated_" or "__ExternalActors__"))
                throw new ArgumentException("Only explicit original standalone PBZ source packages allowed: " + virtualPath);
            var folder = Path.Combine(output, SafeText.Hash(virtualPath)[..20].ToLowerInvariant());
            var geometryPath = Path.Combine(folder, "geometry.json");
            try
            {
                object summary;
                if (!overwrite && File.Exists(geometryPath) && new FileInfo(geometryPath).Length > 0)
                {
                    summary = new { skipped = "existing" };
                }
                else
                {
                    Directory.CreateDirectory(folder);
                    OutputSafety.AtomicWrite(geometryPath, w => session.WriteGeometry(virtualPath, source, w, CancellationToken.None));
                    var doc = JObject.Parse(File.ReadAllText(geometryPath));
                    var lods = (JArray?)doc["lods"] ?? new JArray();
                    int tris = 0, verts = 0;
                    foreach (var l in lods)
                    {
                        verts += ((JArray?)l?["vertices"])?.Count ?? 0;
                        tris += (((JArray?)l?["indices"])?.Count ?? 0) / 3;
                    }
                    summary = new { lodCount = lods.Count, vertices = verts, triangles = tris };
                }
                results.Add(new { virtualPath, source, name, state = "success", geometryPath = geometryPath.Replace('\\', '/'), summary });
            }
            catch (Exception e)
            {
                failed++;
                results.Add(new { virtualPath, source, name, state = "failed", errorType = e.GetType().Name, detail = SafeText.Redact(e.Message) });
            }
            Console.WriteLine(WireJson.Serialize(new { source, phase = "geometry-export", processed = results.Count, total = requests.Count }));
        }
        var report = new { schemaVersion = 1, inputFingerprint = factory.SessionFingerprint, sourceWriteMode = "read-only", total = requests.Count, failed, results };
        OutputSafety.AtomicWrite(Path.Combine(output, "geometry-export-report.json"), w => w.Write(WireJson.Serialize(report)));
        Console.WriteLine(WireJson.Serialize(new { phase = "geometry-export-summary", total = requests.Count, failed }));
        return failed > 0 ? 2 : 0;
    }
}
