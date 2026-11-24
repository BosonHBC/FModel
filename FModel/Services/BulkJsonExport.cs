using System;
using System.Collections.Generic;
using System.Linq;
using System.Threading;
using System.Windows;
using CUE4Parse.FileProvider.Objects;
using FModel.Export;
using FModel.Settings;
using FModel.ViewModels;
using FModel.Views.Resources.Controls;
using Newtonsoft.Json.Linq;
using Serilog;

namespace FModel.Services;

public static class BulkJsonExport
{
    public static async System.Threading.Tasks.Task Execute(ApplicationViewModel view, TreeItem[] folders, GameFile[] selected)
    {
        var settings = UserSettings.Default;
        var config = ConfigLoader.FromGui(JObject.FromObject(settings), JObject.FromObject(settings.CurrentDir));
        var options = new FModel.Export.ExportOptions
        {
            Output = settings.PropertiesDirectory, Existing = settings.JsonExportExisting,
            Workers = settings.JsonExportWorkers, MemoryMb = settings.JsonExportMemoryMb,
            Compact = settings.JsonExportCompact, KeepStructure = settings.KeepDirectoryStructure
        };
        var files = selected.Concat(folders.SelectMany(Collect)).DistinctBy(f => f.Path, StringComparer.OrdinalIgnoreCase).ToArray();
        var packages = files.Where(f => f.Extension is "uasset" or "umap").Select(f => f.Path).ToArray();
        var legacy = files.Where(f => f.Extension is not ("uasset" or "umap")).ToArray();
        await ApplicationService.ThreadWorkerView.Begin(token =>
        {
            try
            {
                if (packages.Length > 0)
                {
                    var runner = new ExportRunner(e =>
                    {
                        Log.Information("JSON export {JobId}: {Phase} {Processed}/{Total}, workers={Workers}, counts={Counts}", e.JobId, e.Phase, e.Processed, e.Total, e.Workers, e.Counts);
                        Application.Current.Dispatcher.BeginInvoke(new Action(() => view.Status.UpdateStatusLabel($"{e.Processed}/{e.Total}; workers={e.Workers}", "JSON export")));
                        if (e.Phase is "started" or "finished")
                            FLogger.Append(ELog.Information, () => FLogger.Text($"JSON export {e.Phase}: {e.Processed}/{e.Total}; workers={e.Workers}", Constants.WHITE, true));
                    });
                    var report = runner.RunAsync(new CueSessionFactory(config), options, null, true, packages, token).GetAwaiter().GetResult();
                    var counts = string.Join(", ", report.Counts.Select(p => $"{p.Key}={p.Value}"));
                    FLogger.Append(report.ExitCode == 0 ? ELog.Information : ELog.Warning,
                        () => FLogger.Text($"JSON export: {counts}; unprocessed={report.Unprocessed}; exit={report.ExitCode}. Report: {options.Output}/.fmodel-export/latest-report.json", Constants.WHITE, true));
                    if (report.ExitCode == 3) throw new OperationCanceledException(token);
                    if (report.ExitCode == 4) throw new InvalidOperationException("JSON task failed; inspect the export report.");
                }
                foreach (var file in legacy)
                {
                    token.ThrowIfCancellationRequested();
                    view.CUE4Parse.Extract(token, file, false, EBulkType.Properties | EBulkType.Auto);
                }
            }
            catch (OperationCanceledException) { throw; }
            catch (Exception e)
            {
                var message = SafeText.Redact(e.Message);
                Log.Error("Bulk JSON export failed: {Message}", message);
                FLogger.Append(ELog.Error, () => FLogger.Text(message, Constants.WHITE, true));
                throw new InvalidOperationException(message);
            }
        });
    }
    private static IEnumerable<GameFile> Collect(TreeItem folder)
    {
        foreach (var entry in folder.AssetsList.Assets) yield return entry.Asset;
        foreach (var child in folder.Folders)
            foreach (var file in Collect(child)) yield return file;
    }
}
