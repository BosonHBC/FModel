using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using CUE4Parse.Compression;
using CUE4Parse.FileProvider;
using CUE4Parse.FileProvider.Vfs;
using CUE4Parse.UE4.Assets.Exports.Material;
using CUE4Parse.UE4.IO;
using CUE4Parse.UE4.IO.Objects;
using CUE4Parse.UE4.Readers;
using CUE4Parse.UE4.Shaders;
using Serilog;

namespace FModel.Framework;

/// <summary>
/// Extracts per-permutation shader bytecode (DXIL/DXBC) for a material from the shared
/// IoStore shader libraries (*.ushaderbytecode), then optionally decompiles them to HLSL
/// via an external HLSL-Decompiler executable.
///
/// Pipeline (validated end-to-end):
///   material -> LoadedMaterialResources[].LoadedShaderMap.ResourceHash (SM6 + SM5)
///   -> match hash in *.ushaderbytecode FIoStoreShaderCodeArchive
///   -> read shader group IoChunk (IoStoreReader.Read) [1st decompress: IoStore layer]
///   -> Oodle-decompress the group blob (CompressedSize -> UncompressedSize) [2nd layer]
///   -> slice each shader by UncompressedOffsetInGroup
///   -> locate 'DXBC' container magic, cut by container size -> clean .dxil/.dxbc
///   -> HLSLDecompiler.exe {file} (-dxil | -dxbc) -> .hlsl
/// </summary>
public static class MaterialShaderExporter
{
    public class Result
    {
        public int DxilDumped;
        public int HlslDecompiled;
        public int HlslFailed;
        public string OutputDirectory = "";
        public readonly List<string> Messages = new();
    }

    /// <summary>
    /// Export all permutation shaders of a material to <paramref name="outputDir"/>.
    /// </summary>
    public static Result Export(AbstractVfsFileProvider provider, UMaterialInterface material, string outputDir, string decompilerExe)
    {
        var result = new Result { OutputDirectory = outputDir };
        Directory.CreateDirectory(outputDir);

        // 1) collect wanted shadermap hashes (per platform) from the material
        var wanted = new List<(string Hash, EShaderPlatform Platform)>();
        if (material.LoadedMaterialResources != null)
        {
            foreach (var res in material.LoadedMaterialResources)
            {
                var sm = res.LoadedShaderMap;
                if (sm?.ResourceHash != null)
                    wanted.Add((sm.ResourceHash.ToString(), sm.ShaderPlatform));
            }
        }

        if (wanted.Count == 0)
        {
            result.Messages.Add("No shader map hashes found on this material. Make sure 'Read Shader Maps' is enabled in Settings, then reload the asset.");
            return result;
        }

        // 2) index all .ushaderbytecode IoStore entries
        var libEntries = provider.Files.Values
            .OfType<FIoStoreEntry>()
            .Where(e => e.Path.EndsWith(".ushaderbytecode", StringComparison.OrdinalIgnoreCase))
            .ToList();

        if (libEntries.Count == 0)
        {
            result.Messages.Add("No .ushaderbytecode shader libraries found in the mounted archives.");
            return result;
        }

        var matName = material.Name;

        foreach (var libEntry in libEntries)
        {
            byte[] libBytes;
            try { libBytes = libEntry.Read(); } catch { continue; }

            FIoStoreShaderCodeArchive io;
            try
            {
                using var ar = new FByteArchive(libEntry.Path, libBytes, provider.Versions);
                var archive = new FShaderCodeArchive(ar);
                io = archive.SerializedShaders as FIoStoreShaderCodeArchive;
                if (io == null) continue;
            }
            catch { continue; }

            var reader = libEntry.IoStoreReader;

            foreach (var (wantHash, platform) in wanted)
            {
                int mapIdx = Array.FindIndex(io.ShaderMapHashes, h => h.ToString() == wantHash);
                if (mapIdx < 0) continue;

                var mapEntry = io.ShaderMapEntries[mapIdx];
                bool isDxil = IsDxilPlatform(platform);
                var groupCache = new Dictionary<uint, byte[]>();

                for (uint s = 0; s < mapEntry.NumShaders; s++)
                {
                    uint shaderIdx = io.ShaderIndices[mapEntry.ShaderIndicesOffset + s];
                    var entry = io.ShaderEntries[shaderIdx];
                    uint groupIndex = entry.ShaderGroupIndex;
                    uint offsetInGroup = entry.UncompressedOffsetInGroup;

                    if (!groupCache.TryGetValue(groupIndex, out var groupBlob))
                    {
                        var groupEntry = io.ShaderGroupEntries[groupIndex];
                        var chunkId = io.ShaderGroupIoHashes[groupIndex];
                        byte[] rawGroup;
                        try { rawGroup = reader.Read(chunkId); }
                        catch (Exception e) { result.Messages.Add($"group {groupIndex}: read chunk failed: {e.Message}"); continue; }

                        if ((uint) rawGroup.Length == groupEntry.UncompressedSize)
                        {
                            groupBlob = rawGroup;
                        }
                        else
                        {
                            try { groupBlob = Compression.Decompress(rawGroup, (int) groupEntry.UncompressedSize, CompressionMethod.Oodle); }
                            catch (Exception e) { result.Messages.Add($"group {groupIndex}: Oodle decompress failed: {e.Message}"); continue; }
                        }
                        groupCache[groupIndex] = groupBlob;
                    }

                    long len = ComputeShaderLength(io, groupIndex, offsetInGroup, groupBlob.Length);
                    if (offsetInGroup >= (uint) groupBlob.Length) continue;
                    len = Math.Min(len, groupBlob.Length - offsetInGroup);

                    var rawBlock = new byte[len];
                    Array.Copy(groupBlob, offsetInGroup, rawBlock, 0, len);

                    int dxbcPos = FindDxbc(rawBlock);
                    if (dxbcPos < 0) continue;

                    int containerSize = (int) BitConverter.ToUInt32(rawBlock, dxbcPos + 24);
                    if (containerSize <= 0 || dxbcPos + containerSize > rawBlock.Length)
                        containerSize = rawBlock.Length - dxbcPos;

                    var dxil = new byte[containerSize];
                    Array.Copy(rawBlock, dxbcPos, dxil, 0, containerSize);

                    var ext = isDxil ? "dxil" : "dxbc";
                    var hashShort = wantHash.Length >= 8 ? wantHash.Substring(0, 8) : wantHash;
                    var baseName = $"{matName}_{hashShort}_{entry.Frequency}_s{s}_g{groupIndex}";
                    var binPath = Path.Combine(outputDir, $"{baseName}.{ext}");
                    File.WriteAllBytes(binPath, dxil);
                    result.DxilDumped++;

                    // 3) optional: decompile to HLSL
                    if (!string.IsNullOrEmpty(decompilerExe) && File.Exists(decompilerExe))
                    {
                        if (RunDecompiler(decompilerExe, binPath, isDxil, out var err))
                            result.HlslDecompiled++;
                        else
                        {
                            result.HlslFailed++;
                            if (!string.IsNullOrEmpty(err)) result.Messages.Add($"{baseName}: {err.Trim()}");
                        }
                    }
                }
            }
        }

        return result;
    }

    private static bool IsDxilPlatform(EShaderPlatform platform)
    {
        // SM6 (and SM6-family) uses DXIL; SM5 uses DXBC.
        var name = platform.ToString();
        return name.Contains("SM6", StringComparison.OrdinalIgnoreCase);
    }

    private static bool RunDecompiler(string exe, string inputFile, bool isDxil, out string error)
    {
        error = "";
        try
        {
            var psi = new ProcessStartInfo
            {
                FileName = exe,
                UseShellExecute = false,
                RedirectStandardError = true,
                RedirectStandardOutput = true,
                CreateNoWindow = true,
                WorkingDirectory = Path.GetDirectoryName(exe) ?? Environment.CurrentDirectory,
            };
            psi.ArgumentList.Add(inputFile);
            psi.ArgumentList.Add(isDxil ? "-dxil" : "-dxbc");

            using var p = Process.Start(psi);
            if (p == null) { error = "failed to start decompiler"; return false; }
            var stderr = p.StandardError.ReadToEnd();
            p.StandardOutput.ReadToEnd();
            p.WaitForExit(120000);

            var hlslPath = Path.ChangeExtension(inputFile, ".hlsl");
            if (p.ExitCode == 0 && File.Exists(hlslPath) && new FileInfo(hlslPath).Length > 0)
                return true;

            error = string.IsNullOrWhiteSpace(stderr) ? $"exit code {p.ExitCode}" : stderr;
            return false;
        }
        catch (Exception e)
        {
            error = e.Message;
            return false;
        }
    }

    // smallest offset in same group that is > offsetInGroup; else end of blob
    private static long ComputeShaderLength(FIoStoreShaderCodeArchive io, uint groupIndex, uint offsetInGroup, int blobLen)
    {
        long next = blobLen;
        var grp = io.ShaderGroupEntries[groupIndex];
        for (uint i = 0; i < grp.NumShaders; i++)
        {
            uint sIdx = io.ShaderIndices[grp.ShaderIndicesOffset + i];
            var e = io.ShaderEntries[sIdx];
            if (e.ShaderGroupIndex != groupIndex) continue;
            long o = e.UncompressedOffsetInGroup;
            if (o > offsetInGroup && o < next) next = o;
        }
        return next - offsetInGroup;
    }

    // scan for the DXBC container magic (44 58 42 43) shared by DXBC (SM5) and DXIL (SM6)
    private static int FindDxbc(byte[] data)
    {
        for (int i = 0; i + 4 <= data.Length; i++)
        {
            if (data[i] == 0x44 && data[i + 1] == 0x58 && data[i + 2] == 0x42 && data[i + 3] == 0x43)
                return i;
        }
        return -1;
    }
}
