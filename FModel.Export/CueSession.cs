using System.Diagnostics;
using CUE4Parse.Compression;
using CUE4Parse.Encryption.Aes;
using CUE4Parse.FileProvider;
using CUE4Parse.MappingsProvider;
using CUE4Parse.MappingsProvider.Usmap;
using CUE4Parse.UE4.Assets.Exports;
using CUE4Parse.UE4.Objects.Core.Misc;
using CUE4Parse.UE4.Versions;
using Newtonsoft.Json;
using OodleDotNet;
using ZlibngDotNet;

namespace FModel.Export;

public sealed class CueSessionFactory : ISessionFactory
{
    private static readonly object NativeLock = new();
    public const bool PbzParallelValidated = true;
    private readonly SessionConfig _config;
    private readonly string _fingerprint;
    public string InputDirectory => _config.InputDirectory;
    public string SessionFingerprint => _fingerprint;
    public bool ParallelSafe => PbzParallelValidated && _config.Game == EGame.GAME_PhantomBladeZero;
    public int MaximumWorkers => ParallelSafe ? 4 : 1;
    public CueSessionFactory(SessionConfig config)
    {
        _config = config with { InputDirectory = Path.GetFullPath(config.InputDirectory), Mapping = Path.GetFullPath(config.Mapping), DataDirectory = Path.GetFullPath(config.DataDirectory) };
        if (!Directory.Exists(InputDirectory)) throw new ArgumentException("Input directory does not exist.");
        if (!File.Exists(_config.Mapping) || !Path.GetExtension(_config.Mapping).Equals(".usmap", StringComparison.OrdinalIgnoreCase))
            throw new ArgumentException("A local .usmap file is required; use --mapping.");
        lock (NativeLock)
        {
            if (OodleHelper.Instance is null)
            {
                var oodle = new[] { OodleHelper.OODLE_NAME_CURRENT, OodleHelper.OODLE_NAME_OLD }
                    .Select(n => Path.Combine(_config.DataDirectory, n)).FirstOrDefault(File.Exists);
                if (oodle is null) throw new ArgumentException("Local Oodle DLL is missing; use --data-directory (no automatic download).");
                OodleHelper.Initialize(new Oodle(oodle));
            }
            if (ZlibHelper.Instance is null)
            {
                var zlib = Path.Combine(_config.DataDirectory, ZlibHelper.DLL_NAME);
                if (!File.Exists(zlib)) throw new ArgumentException("Local zlib-ng2.dll is missing; use --data-directory.");
                ZlibHelper.Initialize(new Zlibng(zlib));
            }
            PropertyUtil.SearchPropertyInTemplate = true;
        }
        var sources = new List<string>();
        foreach (var folder in new[] { InputDirectory }.Concat(_config.ExtraDirectories))
        {
            foreach (var file in Directory.EnumerateFiles(folder, "*", SearchOption.AllDirectories).Order(StringComparer.OrdinalIgnoreCase))
            {
                var ext = Path.GetExtension(file).ToLowerInvariant();
                if (ext is not (".utoc" or ".ucas" or ".pak" or ".uasset" or ".umap" or ".uexp")) continue;
                var info = new FileInfo(file);
                sources.Add($"{info.FullName}|{info.Length}|{info.LastWriteTimeUtc.Ticks}|{(_config.HashInputs || ext == ".utoc" ? SafeText.FileHash(file) : "metadata-only")}");
            }
        }
        _fingerprint = SafeText.Hash(WireJson.Serialize(_config) + SafeText.FileHash(_config.Mapping) +
            SafeText.Hash(WireJson.Serialize(_config.Keys)) + string.Join('\n', sources));
    }
    public IExportSession Create() => new CueSession(_config, _fingerprint);
}

public sealed class CueSession : IExportSession
{
    private readonly DefaultFileProvider _provider;
    internal DefaultFileProvider Provider => _provider;
    public string Fingerprint { get; }
    public CueSession(SessionConfig config, string fingerprint)
    {
        Fingerprint = fingerprint;
        _provider = new DefaultFileProvider(new DirectoryInfo(config.InputDirectory),
            config.ExtraDirectories.Select(d => new DirectoryInfo(d)).ToArray(), SearchOption.AllDirectories,
            config.Versions(), StringComparer.OrdinalIgnoreCase)
        {
            ReadScriptData = config.ReadScriptData,
            ReadShaderMaps = config.ReadShaderMaps,
            ReadNaniteData = true,
            MappingsContainer = new FileUsmapTypeMappingsProvider(config.Mapping)
        };
        try
        {
            _provider.Initialize();
            foreach (var pair in config.Keys)
            {
                var key = pair.Value.StartsWith("0x", StringComparison.OrdinalIgnoreCase) ? pair.Value[2..] : pair.Value;
                if (key.Length != 64 || !key.All(Uri.IsHexDigit)) throw new ArgumentException("Invalid AES key format in local configuration.");
                _provider.SubmitKey(new FGuid(pair.Key.Replace("-", "")), new FAesKey(pair.Value));
            }
            _provider.PostMount();
            if (_provider.RequiredKeys.Count > 0) throw new ArgumentException("Some archives remain encrypted. Configure matching main/dynamic AES keys locally.");
            if (_provider.Files.Count == 0) throw new ArgumentException("No mounted files; check input directory, game version and keys.");
        }
        catch { _provider.Dispose(); throw; }
    }
    public IReadOnlyList<PackageItem> List(string? path, bool recursive, IEnumerable<string>? files = null)
    {
        var packages = _provider.Files.Values.Where(f => !f.IsHidden && f.Extension is "uasset" or "umap")
            .Select(f => new PackageItem(f.Path, f.Size)).DistinctBy(f => f.Path, StringComparer.OrdinalIgnoreCase).ToArray();
        if (files is not null)
        {
            var lookup = packages.ToDictionary(f => f.Path, StringComparer.OrdinalIgnoreCase);
            return files.Where(s => !string.IsNullOrWhiteSpace(s)).Select(s => s.Trim().Replace('\\', '/'))
                .Distinct(StringComparer.OrdinalIgnoreCase).Select(s => lookup.TryGetValue(s, out var file) ? file :
                    throw new ArgumentException($"Package not found: {s}")).OrderBy(f => f.Path, StringComparer.OrdinalIgnoreCase).ToArray();
        }
        var prefix = (path ?? "").Replace('\\', '/').TrimEnd('/');
        if (prefix.StartsWith('/') || prefix.Split('/').Any(s => s is "." or "..")) throw new ArgumentException("Use a virtual package path, not a filesystem or /Game path.");
        var selected = packages.Where(f => prefix.Length == 0 || f.Path.StartsWith(prefix + "/", StringComparison.OrdinalIgnoreCase));
        if (!recursive) selected = selected.Where(f => !f.Path[(prefix.Length == 0 ? 0 : prefix.Length + 1)..].Contains('/'));
        var result = selected.OrderBy(f => f.Path, StringComparer.OrdinalIgnoreCase).ToArray();
        if (result.Length == 0) throw new ArgumentException("No packages match the virtual path. Use --recursive for subdirectories.");
        return result;
    }
    public void WriteJson(string path, TextWriter writer, bool compact, CancellationToken token)
    {
        token.ThrowIfCancellationRequested();
        var package = _provider.LoadPackage(path);
        using var json = new JsonTextWriter(writer) { Formatting = compact ? Formatting.None : Formatting.Indented, CloseOutput = false };
        var serializer = JsonSerializer.CreateDefault();
        json.WriteStartArray();
        foreach (var export in package.GetExports())
        {
            token.ThrowIfCancellationRequested();
            serializer.Serialize(json, export);
        }
        json.WriteEndArray();
        json.Flush();
    }

    /// <summary>
    /// Decode one cooked UStaticMesh into the project's geometry.json schema
    /// (the same shape produced by PayloadExporter/EnvironmentEvidence.cs), so
    /// build_environment_mesh_gltf.py and the UE mesh importer can consume it.
    /// Uses StaticMeshDto with NaniteOnly, matching the verified PBZ pipeline.
    /// Returns a small summary object; throws when the package has no decodable
    /// StaticMesh (caller records it as failed, never silently writes a stub).
    /// </summary>
    public object WriteGeometry(string packagePath, string sourcePath, TextWriter writer, CancellationToken token)
    {
        token.ThrowIfCancellationRequested();
        var package = _provider.LoadPackage(packagePath);
        var mesh = package.GetExports().OfType<CUE4Parse.UE4.Assets.Exports.StaticMesh.UStaticMesh>().FirstOrDefault()
            ?? throw new InvalidDataException("No UStaticMesh export in package");
        using var dto = new CUE4Parse_Conversion.Dto.StaticMeshDto(mesh,
            CUE4Parse_Conversion.Options.EMeshQuality.All,
            CUE4Parse_Conversion.Options.ENaniteMeshFormat.NaniteOnly);
        if (dto.LODs.Count == 0) throw new InvalidDataException("No source mesh geometry decoded");
        var lods = new List<object>();
        foreach (var lod in dto.LODs)
        {
            if (lod.Vertices.Length == 0 || lod.Indices.Length == 0 || lod.Indices.Length % 3 != 0
                || lod.Indices.Any(i => i >= lod.Vertices.Length))
                throw new InvalidDataException("Invalid decoded vertex/index payload");
            lods.Add(new
            {
                sourceLodIndex = lod.SourceLodIndex,
                isNaniteDecoded = lod.IsNanite,
                vertices = lod.Vertices.Select(v => new
                {
                    position = new[] { v.Position.X, v.Position.Y, v.Position.Z },
                    normal = new[] { v.Normal.X, v.Normal.Y, v.Normal.Z, v.Normal.W },
                    tangent = new[] { v.Tangent.X, v.Tangent.Y, v.Tangent.Z, v.Tangent.W },
                    uv0 = new[] { v.Uv.U, v.Uv.V }
                }).ToArray(),
                indices = lod.Indices,
                extraUVs = lod.ExtraUvs,
                colors = lod.VertexColors,
                sections = lod.Sections,
                bounds = lod.CalculateLodBounds(),
                screenSize = lod.ScreenSize,
                precision = "Decoded cooked Nanite/LOD, not original DCC mesh; synthesized missing tangents identified by zero vector"
            });
        }
        var snapshot = Newtonsoft.Json.Linq.JToken.FromObject(mesh);
        var doc = new
        {
            schemaVersion = 1,
            source = sourcePath,
            inputFingerprint = Fingerprint,
            coordinates = "UE centimeters, original axes",
            materials = snapshot["Properties"]?["StaticMaterials"],
            lods
        };
        writer.Write(WireJson.Serialize(doc));
        return new { lodCount = dto.LODs.Count, vertices = dto.LODs.Sum(l => l.Vertices.Length), triangles = dto.LODs.Sum(l => l.Indices.Length / 3) };
    }
    public string PackageFingerprint(PackageItem item) => SafeText.Hash(Fingerprint + "|" + item.Path + "|" + item.Size);
    public void ClearCache() => _provider.TextureCachePaths.Clear();
    public void Dispose() => _provider.Dispose();
}
