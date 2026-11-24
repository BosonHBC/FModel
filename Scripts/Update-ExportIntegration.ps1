$ErrorActionPreference = 'Stop'
function Edit($path, $old, $new) {
    $text = [IO.File]::ReadAllText($path)
    if (!$text.Contains($old)) { throw "Expected source anchor not found: $path" }
    [IO.File]::WriteAllText($path, $text.Replace($old, $new))
}
Edit 'I:/FModel/FModel.Export/CueSession.cs' 'using CUE4Parse.MappingsProvider;' "using CUE4Parse.MappingsProvider;`nusing CUE4Parse.MappingsProvider.Usmap;"
Edit 'I:/FModel/FModel.Export/CueSession.cs' 'public string InputDirectory => _config.InputDirectory;' "public string InputDirectory => _config.InputDirectory;`n    public string SessionFingerprint => _fingerprint;"
Edit 'I:/FModel/FModel.Export/CueSession.cs' @'
foreach (var pair in config.Keys)
                _provider.SubmitKey(new FGuid(pair.Key.Replace("-", "")), new FAesKey(pair.Value));
'@ @'
foreach (var pair in config.Keys)
            {
                var key = pair.Value.StartsWith("0x", StringComparison.OrdinalIgnoreCase) ? pair.Value[2..] : pair.Value;
                if (key.Length != 64 || !key.All(Uri.IsHexDigit)) throw new ArgumentException("Invalid AES key format in local configuration.");
                _provider.SubmitKey(new FGuid(pair.Key.Replace("-", "")), new FAesKey(pair.Value));
            }
'@
Edit 'I:/FModel/FModel.Export/ExportRunner.cs' @'
if (ShouldSkip(item, out var result)) Record(result);
                    else await pending.Writer.WriteAsync(item, token);
'@ @'
bool skip;
                    PackageResult result;
                    lock (sync) skip = ShouldSkip(item, out result);
                    if (skip) Record(result);
                    else await pending.Writer.WriteAsync(item, token);
'@
Edit 'I:/FModel/FModel.Cli/Program.cs' 'Console.WriteLine(Help);' 'Console.WriteLine(CliHelp.Text);'
Edit 'I:/FModel/FModel.Cli/Program.cs' 'const string Help = """' "internal static class CliHelp`n{`npublic const string Text = `"`"`""
Add-Content 'I:/FModel/FModel.Cli/Program.cs' '}'
Edit 'I:/FModel/FModel/FModel.csproj' '<ProjectReference Include="..\CUE4Parse\CUE4Parse-Conversion\CUE4Parse-Conversion.csproj" />' "<ProjectReference Include=`"..\CUE4Parse\CUE4Parse-Conversion\CUE4Parse-Conversion.csproj`" />`n    <ProjectReference Include=`"..\FModel.Export\FModel.Export.csproj`" />"
Edit 'I:/FModel/FModel/FModel.slnx' '<Project Path="FModel.csproj" />' "<Project Path=`"FModel.csproj`" />`n  <Project Path=`"../FModel.Export/FModel.Export.csproj`" />`n  <Project Path=`"../FModel.Cli/FModel.Cli.csproj`" />`n  <Project Path=`"../FModel.Export.Tests/FModel.Export.Tests.csproj`" />"
