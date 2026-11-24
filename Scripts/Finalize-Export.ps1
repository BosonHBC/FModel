$ErrorActionPreference = 'Stop'
function Edit($path, $old, $new) {
    $text = [IO.File]::ReadAllText($path)
    if (!$text.Contains($old)) { throw "Source anchor not found: $path" }
    [IO.File]::WriteAllText($path, $text.Replace($old, $new))
}
$stress = Get-Content 'I:/PBZ/Analysis/ExportStress/20261102-172358/stress-results.json' -Raw | ConvertFrom-Json
if ($stress.runs.Count -ne 3 -or @($stress.runs | Where-Object semanticMatches -ne 72).Count -gt 0) { throw 'Parallel validation is incomplete' }
Edit 'I:/FModel/FModel.Export/Contracts.cs' 'bool ParallelSafe { get; }' "bool ParallelSafe { get; }`n    int MaximumWorkers => ParallelSafe ? 32 : 1;"
Edit 'I:/FModel/FModel.Export/CueSession.cs' 'public const bool PbzParallelValidated = false;' 'public const bool PbzParallelValidated = true;'
Edit 'I:/FModel/FModel.Export/CueSession.cs' 'public bool ParallelSafe => PbzParallelValidated && _config.Game == EGame.GAME_PhantomBladeZero;' "public bool ParallelSafe => PbzParallelValidated && _config.Game == EGame.GAME_PhantomBladeZero;`n    public int MaximumWorkers => ParallelSafe ? 4 : 1;"
Edit 'I:/FModel/FModel.Export/ExportRunner.cs' 'ActualWorkers = factory.ParallelSafe ? options.Workers : 1,' 'ActualWorkers = factory.ParallelSafe ? Math.Min(options.Workers, factory.MaximumWorkers) : 1,'
Edit 'I:/FModel/FModel.Export/ExportRunner.cs' 'if (report.ActualWorkers != options.Workers) report.Diagnostics.Add("Parallel parsing has not been validated for this preset; using one worker.");' 'if (report.ActualWorkers != options.Workers) report.Diagnostics.Add($"Requested workers limited to {report.ActualWorkers} by validated preset safety limits.");'
Edit 'I:/FModel/FModel.Export/ExportRunner.cs' 'report.Diagnostics.AddRange(jobDiagnostics.Distinct());' "if (report.PeakMemoryBytes > budget) report.Diagnostics.Add(`"Soft memory budget exceeded; large packages can require an exclusive worker beyond the configured budget.`");`n            report.Diagnostics.AddRange(jobDiagnostics.Distinct());"
Edit 'I:/FModel/FModel/Services/BulkJsonExport.cs' 'FLogger.Append(ELog.Error, () => FLogger.Text(message, Constants.WHITE, true));' "FLogger.Append(ELog.Error, () => FLogger.Text(message, Constants.WHITE, true));`n                throw new InvalidOperationException(message);"
Edit 'I:/FModel/FModel.Export.Tests/Program.cs' 'File.WriteAllText(Path.Combine(root, "test-results.json"), WireJson.Serialize(new { passed = checks.Count, checks, root }));' @'
ConfigurationChecks.Run(root, Check);
if (args.Contains("--cli")) await ProcessChecks.Run(args[Array.IndexOf(args, "--cli") + 1], root, Check);
File.WriteAllText(Path.Combine(root, "test-results.json"), WireJson.Serialize(new { passed = checks.Count, checks, root }));
'@
Edit 'I:/FModel/FModel/FModel.slnx' '<Project Path="../FModel.Export.Tests/FModel.Export.Tests.csproj" />' "<Project Path=`"../FModel.Export.Tests/FModel.Export.Tests.csproj`" />`n  <Project Path=`"../FModel.Export.Benchmarks/FModel.Export.Benchmarks.csproj`" />"
