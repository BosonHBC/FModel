using System.Text;
using Newtonsoft.Json;

namespace FModel.Export;

public static class OutputSafety
{
    public static bool Inside(string child, string parent)
    {
        child = Path.GetFullPath(child).TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
        parent = Path.GetFullPath(parent).TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
        return child.Equals(parent, StringComparison.OrdinalIgnoreCase) || child.StartsWith(parent + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase);
    }
    public static void Validate(string output, string input)
    {
        var gameRoot = Path.GetFullPath(input);
        var folder = new DirectoryInfo(gameRoot);
        if (folder.Name.Equals("Paks", StringComparison.OrdinalIgnoreCase) && folder.Parent?.Name.Equals("Content", StringComparison.OrdinalIgnoreCase) == true)
            gameRoot = folder.Parent.Parent?.Parent?.FullName ?? gameRoot;
        if (Inside(output, gameRoot)) throw new ArgumentException("Output/report/manifest must not be inside the game installation.");
        if (File.Exists(output) && (File.GetAttributes(output) & FileAttributes.ReparsePoint) != 0)
            throw new ArgumentException("Reparse-point report/manifest files are not supported.");
        for (var dir = new DirectoryInfo(Path.GetFullPath(output)); dir is not null; dir = dir.Parent)
            if (dir.Exists && (dir.Attributes & FileAttributes.ReparsePoint) != 0)
                throw new ArgumentException("Reparse-point output paths are not supported for safe containment.");
    }
    public static string Target(string output, string package, bool structure)
    {
        var pieces = package.Replace('\\', '/').Split('/');
        if (pieces.Any(p => p.Length == 0 || p is "." or ".." || p.IndexOfAny(Path.GetInvalidFileNameChars()) >= 0 || p.EndsWith('.') || p.EndsWith(' ')))
            throw new ArgumentException("Invalid package output path.");
        var relative = structure ? Path.Combine(pieces) : pieces[^1];
        var target = Path.GetFullPath(Path.Combine(output, Path.ChangeExtension(relative, ".json")));
        if (!Inside(target, output)) throw new ArgumentException("Path escapes output root.");
        for (var dir = new DirectoryInfo(Path.GetDirectoryName(target)!); dir is not null && Inside(dir.FullName, output); dir = dir.Parent)
            if (dir.Exists && (dir.Attributes & FileAttributes.ReparsePoint) != 0) throw new ArgumentException("Output contains a reparse point.");
        if (File.Exists(target) && (File.GetAttributes(target) & FileAttributes.ReparsePoint) != 0) throw new ArgumentException("Output is a reparse point.");
        return target;
    }
    public static void AtomicWrite(string target, Action<TextWriter> action, CancellationToken token = default)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(Path.GetFullPath(target))!);
        var temp = target + "." + Guid.NewGuid().ToString("N") + ".tmp";
        try
        {
            using (var stream = new FileStream(temp, FileMode.CreateNew, FileAccess.Write, FileShare.None, 65536))
            {
                using (var writer = new StreamWriter(stream, new UTF8Encoding(false), 65536, true)) action(writer);
                token.ThrowIfCancellationRequested();
                stream.Flush(true);
            }
            File.Move(temp, target, true);
        }
        finally { if (File.Exists(temp)) File.Delete(temp); }
    }
    public static bool ValidJson(string target)
    {
        try
        {
            using var file = File.OpenText(target);
            using var json = new JsonTextReader(file) { SupportMultipleContent = true };
            if (!json.Read() || json.TokenType != JsonToken.StartArray) return false;
            while (json.Read())
                if (json.TokenType == JsonToken.EndArray && json.Depth == 0) return !json.Read();
            return false;
        }
        catch (JsonException) { return false; }
    }
    public static IDisposable AcquireLock(string output)
    {
        Directory.CreateDirectory(output);
        try { return new FileStream(Path.Combine(output, ".fmodel-export.lock"), FileMode.OpenOrCreate, FileAccess.ReadWrite, FileShare.None); }
        catch (IOException e) { throw new IOException("Another task is using this output directory, or it is not writable.", e); }
    }
}

public sealed class ManifestStore : IDisposable
{
    private readonly StreamWriter _writer;
    public Dictionary<string, PackageResult> Records { get; } = new(StringComparer.OrdinalIgnoreCase);
    public ManifestStore(string path)
    {
        Directory.CreateDirectory(Path.GetDirectoryName(Path.GetFullPath(path))!);
        if (File.Exists(path))
        {
            var lines = File.ReadLines(path).GetEnumerator();
            string? previous = null;
            while (lines.MoveNext())
            {
                if (previous is not null) ReadRecord(previous, false);
                previous = lines.Current;
            }
            var damagedTail = previous is not null && !ReadRecord(previous, true);
            lines.Dispose();
            if (damagedTail)
            {
                using var file = new FileStream(path, FileMode.Open, FileAccess.ReadWrite, FileShare.None);
                var position = file.Length - 1;
                while (position >= 0)
                {
                    file.Position = position;
                    var current = file.ReadByte();
                    if (current is not ('\n' or '\r')) break;
                    position--;
                }
                while (position >= 0)
                {
                    file.Position = position;
                    if (file.ReadByte() == '\n') break;
                    position--;
                }
                file.SetLength(position + 1);
            }
        }
        _writer = new StreamWriter(new FileStream(path, FileMode.Append, FileAccess.Write, FileShare.Read), new UTF8Encoding(false));
        _writer.WriteLine();
        _writer.Flush();
    }
    private bool ReadRecord(string line, bool final)
    {
        if (string.IsNullOrWhiteSpace(line)) return true;
        try
        {
            var record = JsonConvert.DeserializeObject<PackageResult>(line);
            if (record?.Path is not null) Records[record.Path] = record;
            return true;
        }
        catch (JsonException)
        {
            if (!final) throw new InvalidDataException("Manifest has a malformed non-final record; use a new manifest.");
            return false;
        }
    }
    public void Append(PackageResult result)
    {
        _writer.WriteLine(WireJson.Serialize(result));
        _writer.Flush();
        Records[result.Path] = result;
    }
    public void Dispose() => _writer.Dispose();
}
