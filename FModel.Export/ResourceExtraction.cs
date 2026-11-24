using System.Buffers.Binary;
using System.Diagnostics;
using System.Security.Cryptography;
using CUE4Parse.UE4.Assets.Exports;
using CUE4Parse.UE4.Assets.Exports.Actor;
using CUE4Parse.UE4.Assets.Exports.Component.Landscape;
using CUE4Parse.UE4.Assets.Exports.Texture;
using CUE4Parse.UE4.Versions;
using Newtonsoft.Json;
using Newtonsoft.Json.Linq;

namespace FModel.Export;

public sealed record ResourceFile(string Path, long Length, string Sha256);
public sealed record ResourceResult(string Path, string State, int Components, ResourceFile[] Files, string[] Diagnostics);

public static class LandscapePixels
{
    public static int Texel(int vertex, int subsectionQuads)
    {
        if (vertex < 0 || subsectionQuads < 1) throw new ArgumentOutOfRangeException();
        return vertex + (vertex == 0 ? 0 : (vertex - 1) / subsectionQuads);
    }
    public static byte[] Height(byte[] bgra, int textureWidth, int textureHeight, int offsetX, int offsetY, int componentQuads, int subsectionQuads)
    {
        var side = checked(componentQuads + 1);
        var data = new byte[checked(side * side * 2)];
        for (var y = 0; y < side; y++)
        for (var x = 0; x < side; x++)
        {
            var i = Pixel(bgra, textureWidth, textureHeight, offsetX + Texel(x, subsectionQuads), offsetY + Texel(y, subsectionQuads));
            BinaryPrimitives.WriteUInt16LittleEndian(data.AsSpan((y * side + x) * 2, 2), (ushort)((bgra[i + 2] << 8) | bgra[i + 1]));
        }
        return data;
    }
    public static byte[] Weight(byte[] bgra, int textureWidth, int textureHeight, int offsetX, int offsetY, int componentQuads, int subsectionQuads, int rgbaChannel)
    {
        if (rgbaChannel is < 0 or > 3) throw new ArgumentOutOfRangeException(nameof(rgbaChannel));
        int[] offsets = [2, 1, 0, 3];
        var side = checked(componentQuads + 1);
        var data = new byte[checked(side * side)];
        for (var y = 0; y < side; y++)
        for (var x = 0; x < side; x++)
        {
            var i = Pixel(bgra, textureWidth, textureHeight, offsetX + Texel(x, subsectionQuads), offsetY + Texel(y, subsectionQuads));
            data[y * side + x] = bgra[i + offsets[rgbaChannel]];
        }
        return data;
    }
    private static int Pixel(byte[] data, int width, int height, int x, int y)
    {
        if (width < 1 || height < 1 || data.Length != checked(width * height * 4) || x < 0 || y < 0 || x >= width || y >= height)
            throw new InvalidDataException("Landscape pixel coordinates/payload are inconsistent.");
        return checked((y * width + x) * 4);
    }
}

public static class ResourceExtraction
{
    public static int Run(CueSessionFactory factory, string command, string output, string? path, bool recursive,
        IEnumerable<string>? files, string existing, bool includeRaw, string? reportPath, string? cancelFile,
        Action<object> emit, CancellationToken token)
    {
        if (existing is not ("skip" or "overwrite")) throw new ArgumentException("Resource extraction supports --existing skip|overwrite; skip verifies all output hashes.");
        output = System.IO.Path.GetFullPath(output);
        reportPath = System.IO.Path.GetFullPath(reportPath ?? System.IO.Path.Combine(output, "resource-report.json"));
        OutputSafety.Validate(output, factory.InputDirectory);
        OutputSafety.Validate(reportPath, factory.InputDirectory);
        if (cancelFile is not null && OutputSafety.Inside(cancelFile, output)) throw new ArgumentException("Place the cancellation file outside the output directory.");
        using var outputLock = OutputSafety.AcquireLock(output);
        using var listing = factory.Create();
        var selected = listing.List(path, recursive, files);
        var results = new List<ResourceResult>();
        var watch = Stopwatch.StartNew();
        var exit = 0;
        CueSession? session = null;
        try
        {
            emit(new { schemaVersion = 1, phase = "resource-start", command, total = selected.Count, workers = 1 });
            for (var n = 0; n < selected.Count; n++)
            {
                if (token.IsCancellationRequested || cancelFile is not null && File.Exists(cancelFile)) { exit = 3; break; }
                var item = selected[n];
                var marker = System.IO.Path.ChangeExtension(OutputSafety.Target(output, item.Path, true), ".resource.json");
                var signature = SafeText.Hash(factory.SessionFingerprint + "|resource-1|" + command + "|" + includeRaw + "|" + item.Path);
                if (existing == "skip" && Verified(marker, output, signature))
                {
                    var saved = JObject.Parse(File.ReadAllText(marker));
                    results.Add(new ResourceResult(item.Path, "skipped", saved.Value<int>("components"), [], []));
                    continue;
                }
                if (session is null || n % 256 == 0)
                {
                    session?.Dispose();
                    session = (CueSession)factory.Create();
                    GC.Collect();
                }
                using var diagnostics = new DiagnosticScope();
                var written = new List<ResourceFile>();
                var components = 0;
                var state = "success";
                try
                {
                    if (command == "export-raw" || includeRaw)
                    {
                        foreach (var (name, bytes) in session.Provider.SavePackage(item.Path))
                            Save(output, "raw/" + name, bytes, written, token);
                    }
                    if (command == "export-landscape")
                        components = ExportLandscape(session, item.Path, output, written, token);
                    token.ThrowIfCancellationRequested();
                    if (diagnostics.Messages.Count > 0) state = "partial";
                    var markerData = new { schemaVersion = 1, tool = "resource-1", signature, command, source = item.Path, state, components, files = written, diagnostics = diagnostics.Messages };
                    OutputSafety.AtomicWrite(marker, w => w.Write(WireJson.Serialize(markerData)), token);
                }
                catch (OperationCanceledException) { state = "cancelled"; exit = 3; }
                catch (Exception e) { state = "failed"; diagnostics.Messages.Add(SafeText.Redact(e.GetType().Name + ": " + e.Message)); exit = 2; }
                if (state == "partial") exit = Math.Max(exit, 2);
                results.Add(new ResourceResult(item.Path, state, components, written.ToArray(), diagnostics.Messages.ToArray()));
                if (state is "partial" or "failed" || n % 100 == 0 || n == selected.Count - 1)
                    emit(new { schemaVersion = 1, phase = "resource-progress", processed = results.Count, total = selected.Count, components = results.Sum(r => r.Components), state, package = item.Path, elapsedSeconds = watch.Elapsed.TotalSeconds });
                session.ClearCache();
                if (exit == 3) break;
            }
        }
        finally { session?.Dispose(); }
        var report = new { schemaVersion = 1, command, output, inputFingerprint = factory.SessionFingerprint, exitCode = exit, total = selected.Count,
            unprocessed = selected.Count - results.Count, components = results.Sum(x => x.Components), counts = results.GroupBy(x => x.State).ToDictionary(x => x.Key, x => x.Count()),
            elapsedSeconds = watch.Elapsed.TotalSeconds, limitations = new[] { "Cooked source data, not original editor edit layers or material graphs.", "Height is base stored mip0; PBZ edge-fixup snapshots are preserved but NOT applied.", "Raw IoStore package bytes are not converted to editor-importable uassets.", "RVT runtime page contents and runtime material changes are not captured." }, results };
        OutputSafety.AtomicWrite(reportPath, w => w.Write(WireJson.Serialize(report)));
        emit(new { schemaVersion = 1, phase = "summary", report.exitCode, report.total, report.unprocessed, report.components, report.counts, report.elapsedSeconds, reportPath });
        return exit;
    }

    private static int ExportLandscape(CueSession session, string packagePath, string output, List<ResourceFile> written, CancellationToken token)
    {
        var objects = session.Provider.LoadPackage(packagePath).GetExports().ToArray();
        var folder = System.IO.Path.ChangeExtension(packagePath, null) + "/";
        var metadata = new List<object>();
        var textureMetadata = new List<object>();
        var cachedTextures = new Dictionary<UTexture2D, (byte[] Data, int Width, int Height)>();
        var components = 0;
        (byte[] Data, int Width, int Height) ReadTexture(UTexture2D texture)
        {
            if (cachedTextures.TryGetValue(texture, out var cached)) return cached;
            if (session.Provider.Versions.Platform != ETexturePlatform.DesktopMobile)
                throw new NotSupportedException("Landscape pixel extraction currently requires DesktopMobile (linear desktop textures); raw export supports other platforms.");
            if (texture.Format != EPixelFormat.PF_B8G8R8A8 || texture.PlatformData.VTData is not null)
                throw new NotSupportedException("Landscape pixel extraction requires non-VT PF_B8G8R8A8; original payload can still be exported with export-raw.");
            var mip = texture.GetMip(0) ?? throw new InvalidDataException("Landscape mip0 is missing; refusing to substitute a lower resolution.");
            var pixels = mip.BulkData?.Data ?? throw new InvalidDataException("Landscape mip0 payload is missing.");
            if (pixels.Length != checked(mip.SizeX * mip.SizeY * 4)) throw new InvalidDataException("Landscape mip0 byte length does not match dimensions.");
            var ti = Array.IndexOf(objects, texture);
            if (ti < 0) throw new NotSupportedException("Cross-package Landscape texture: use export-raw for that referenced package.");
            using var mips = new MemoryStream();
            var ranges = new List<object>();
            for (var mi = 0; mi < texture.PlatformData.Mips.Length; mi++)
            {
                token.ThrowIfCancellationRequested();
                var m = texture.PlatformData.Mips[mi];
                var data = m.BulkData?.Data ?? throw new InvalidDataException("A serialized Landscape mip payload is missing.");
                ranges.Add(new { mip = mi, offset = mips.Position, length = data.Length, width = m.SizeX, height = m.SizeY, depth = m.SizeZ });
                mips.Write(data);
            }
            var payloadFile = folder + $"texture-{ti}.mips.bgra";
            Save(output, payloadFile, mips.ToArray(), written, token);
            textureMetadata.Add(new { exportIndex = ti, name = texture.Name, pixelFormat = texture.Format.ToString(), firstMipToSerialize = texture.PlatformData.FirstMipToSerialize, payloadFile, mips = ranges });
            var result = (pixels, mip.SizeX, mip.SizeY);
            cachedTextures[texture] = result;
            return result;
        }
        for (var index = 0; index < objects.Length; index++)
        {
            token.ThrowIfCancellationRequested();
            var obj = objects[index];
            if (obj is not ULandscapeComponent component) continue;
            components++;
            if (component.ComponentSizeQuads <= 0 || component.SubsectionSizeQuads <= 0 || component.ComponentSizeQuads != component.SubsectionSizeQuads * component.NumSubsections)
                throw new InvalidDataException("Landscape component subdivision is inconsistent.");
            var heightTexture = component.GetHeightmap() ?? throw new InvalidDataException("Heightmap texture reference is unresolved.");
            var h = ReadTexture(heightTexture);
            var hx = (int)(h.Width * component.HeightmapScaleBias.Z);
            var hy = (int)(h.Height * component.HeightmapScaleBias.W);
            var heightFile = folder + $"component-{index}.height.r16";
            var heightData = LandscapePixels.Height(h.Data, h.Width, h.Height, hx, hy, component.ComponentSizeQuads, component.SubsectionSizeQuads);
            Save(output, heightFile, heightData, written, token);
            var weights = new List<object>();
            var textures = component.GetWeightmapTextures();
            var allocations = component.GetWeightmapLayerAllocations();
            for (var li = 0; li < allocations.Length; li++)
            {
                var allocation = allocations[li];
                if (allocation.WeightmapTextureIndex >= textures.Length) throw new InvalidDataException("Weightmap texture index is out of range.");
                var wt = textures[allocation.WeightmapTextureIndex];
                var w = ReadTexture(wt);
                var wx = (int)(w.Width * component.WeightmapScaleBias.Z);
                var wy = (int)(w.Height * component.WeightmapScaleBias.W);
                var data = LandscapePixels.Weight(w.Data, w.Width, w.Height, wx, wy, component.ComponentSizeQuads, component.SubsectionSizeQuads, allocation.WeightmapTextureChannel);
                var weightFile = folder + $"component-{index}.layer-{li}.r8";
                Save(output, weightFile, data, written, token);
                var layer = allocation.LayerInfo.Load<ULandscapeLayerInfoObject>() ?? throw new InvalidDataException("LayerInfo reference is unresolved.");
                weights.Add(new { allocationIndex = li, layerName = layer.LayerName.Text, layerInfo = JToken.FromObject(allocation.LayerInfo), layerData = JToken.FromObject(layer), textureIndex = allocation.WeightmapTextureIndex,
                    channel = "RGBA"[allocation.WeightmapTextureChannel].ToString(), weightFile, min = data.Min(), max = data.Max(), nonzeroPixels = data.Count(b => b != 0), sha256 = Convert.ToHexString(SHA256.HashData(data)) });
            }
            string? grassFile = null;
            var grass = component.GrassData;
            if (grass?.HeightWeightData is { Length: > 0 } grassBytes)
            {
                grassFile = folder + $"component-{index}.grass.bin";
                Save(output, grassFile, grassBytes, written, token);
            }
            metadata.Add(new { exportIndex = index, name = component.Name, sectionBaseX = component.SectionBaseX, sectionBaseY = component.SectionBaseY,
                componentQuads = component.ComponentSizeQuads, subsectionQuads = component.SubsectionSizeQuads, numSubsections = component.NumSubsections,
                width = component.ComponentSizeQuads + 1, height = component.ComponentSizeQuads + 1, heightFile, heightByteOrder = "little-endian uint16 row-major; localZ=(H-32768)/128",
                heightTextureIndex = Array.IndexOf(objects, heightTexture), heightOffsetX = hx, heightOffsetY = hy, weights, grass = new { numElements = grass?.NumElements, grassFile,
                    offsets = grass?.WeightOffsets?.Select(kv => new { type = JToken.FromObject(kv.Key), offset = kv.Value }).ToArray() } });
        }
        var preserved = objects.Select((o, i) => new { o, i }).Where(x => x.o is ALandscapeProxy || x.o.ExportType.StartsWith("Landscape", StringComparison.Ordinal) || x.o.ExportType == "SceneComponent")
            .Select(x => new { exportIndex = x.i, data = JToken.FromObject(x.o) }).ToArray();
        var meta = new { schemaVersion = 1, source = packagePath, components = metadata, textures = textureMetadata, objects = preserved,
            note = "Base cooked mip0 heights are unpatched. Preserve/apply PBZ LandscapeHeightmapTextureEdgeFixup separately before asserting runtime-seam-equivalent geometry." };
        var metaPath = OutputSafety.Target(output, packagePath, true);
        OutputSafety.AtomicWrite(metaPath, w => w.Write(WireJson.Serialize(meta)), token);
        written.Add(new ResourceFile(System.IO.Path.GetRelativePath(output, metaPath).Replace('\\', '/'), new FileInfo(metaPath).Length, SafeText.FileHash(metaPath)));
        return components;
    }

    private static string Target(string output, string relative)
    {
        var probe = OutputSafety.Target(output, relative, true);
        var target = System.IO.Path.Combine(System.IO.Path.GetDirectoryName(probe)!, System.IO.Path.GetFileName(relative));
        if (!OutputSafety.Inside(target, output)) throw new ArgumentException("Resource path escapes output.");
        if (File.Exists(target) && File.GetAttributes(target).HasFlag(FileAttributes.ReparsePoint)) throw new ArgumentException("Resource output is a reparse point.");
        return target;
    }
    private static void Save(string output, string relative, byte[] data, List<ResourceFile> files, CancellationToken token)
    {
        var target = Target(output, relative);
        Directory.CreateDirectory(System.IO.Path.GetDirectoryName(target)!);
        var temp = target + "." + Guid.NewGuid().ToString("N") + ".tmp";
        try
        {
            using (var stream = new FileStream(temp, FileMode.CreateNew, FileAccess.Write, FileShare.None)) { stream.Write(data); token.ThrowIfCancellationRequested(); stream.Flush(true); }
            File.Move(temp, target, true);
        }
        finally { if (File.Exists(temp)) File.Delete(temp); }
        files.Add(new ResourceFile(relative, data.LongLength, Convert.ToHexString(SHA256.HashData(data))));
    }
    private static bool Verified(string marker, string output, string signature)
    {
        if (!File.Exists(marker)) return false;
        try
        {
            var m = JObject.Parse(File.ReadAllText(marker));
            if (m.Value<string>("signature") != signature || m.Value<string>("state") != "success") return false;
            if (m["files"] is not JArray { Count: > 0 } recordedFiles) return false;
            foreach (var item in recordedFiles)
            {
                var target = Target(output, item.Value<string>("path")!);
                if (!File.Exists(target) || new FileInfo(target).Length != item.Value<long>("length") || !SafeText.FileHash(target).Equals(item.Value<string>("sha256"), StringComparison.OrdinalIgnoreCase)) return false;
            }
            return true;
        }
        catch (Exception e) when (e is IOException or JsonException or ArgumentException) { return false; }
    }
}
