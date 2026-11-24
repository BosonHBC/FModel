using System.Collections.Concurrent;
using System.Diagnostics;
using System.Threading.Channels;

namespace FModel.Export;

public sealed class ExportRunner
{
    private readonly Action<ExportEvent>? _events;
    public ExportRunner(Action<ExportEvent>? events = null) => _events = events;
    public async Task<ExportReport> RunAsync(ISessionFactory factory, ExportOptions options, string? path,
        bool recursive, IEnumerable<string>? files, CancellationToken cancellationToken = default)
    {
        if (options.Existing is not ("skip" or "resume" or "overwrite")) throw new ArgumentException("existing must be skip, resume or overwrite.");
        if (options.Workers is < 1 or > 32 || options.MemoryMb < 256) throw new ArgumentException("workers must be 1..32; memory-mb must be at least 256.");
        var output = Path.GetFullPath(options.Output);
        OutputSafety.Validate(output, factory.InputDirectory);
        using var rootLock = OutputSafety.AcquireLock(output);
        var reportPath = Path.GetFullPath(options.Report ?? Path.Combine(output, ".fmodel-export", "latest-report.json"));
        var manifestPath = Path.GetFullPath(options.Manifest ?? Path.Combine(output, ".fmodel-export", "manifest.jsonl"));
        OutputSafety.Validate(reportPath, factory.InputDirectory);
        OutputSafety.Validate(manifestPath, factory.InputDirectory);
        if (reportPath.Equals(manifestPath, StringComparison.OrdinalIgnoreCase)) throw new ArgumentException("Report and manifest paths must differ.");
        var jobDiagnostics = new ConcurrentQueue<string>();
        using var taskScope = DiagnosticScope.BeginTask(jobDiagnostics);
        using var linked = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        var token = linked.Token;
        var timer = Stopwatch.StartNew();
        using var first = factory.Create();
        token.ThrowIfCancellationRequested();
        var items = first.List(path, recursive, files);
        if (items.Count == 0) throw new ArgumentException("No packages selected.");
        var targets = items.ToDictionary(x => x.Path, x => OutputSafety.Target(output, x.Path, options.KeepStructure), StringComparer.OrdinalIgnoreCase);
        if (targets.Values.Distinct(StringComparer.OrdinalIgnoreCase).Count() != targets.Count) throw new ArgumentException("Selected packages have colliding JSON filenames; keep directory structure or select fewer packages.");
        if (targets.Values.Any(t => t.Equals(reportPath, StringComparison.OrdinalIgnoreCase) || t.Equals(manifestPath, StringComparison.OrdinalIgnoreCase)))
            throw new ArgumentException("Report or manifest collides with an asset output.");
        var report = new ExportReport
        {
            Output = output, SessionFingerprint = first.Fingerprint, Existing = options.Existing,
            Total = items.Count, RequestedWorkers = options.Workers,
            ActualWorkers = factory.ParallelSafe ? Math.Min(options.Workers, factory.MaximumWorkers) : 1,
            InitializeMilliseconds = timer.Elapsed.TotalMilliseconds
        };
        if (report.ActualWorkers != options.Workers) report.Diagnostics.Add($"Requested workers limited to {report.ActualWorkers} by validated preset safety limits.");
        var budget = (long)options.MemoryMb * 1024 * 1024;
        var before = Process.GetCurrentProcess().WorkingSet64;
        if (before > budget / 2 && report.ActualWorkers > 1)
        {
            report.ActualWorkers = 1;
            report.Diagnostics.Add("Initial working set exceeds half of memory budget; reduced to one worker.");
        }
        using var manifest = new ManifestStore(manifestPath);
        var sync = new object();
        var fatal = new ConcurrentQueue<string>();
        var failedFast = false;
        var exportStart = timer.Elapsed.TotalMilliseconds;
        var gate = new MemoryGate(budget, report.ActualWorkers);
        var pending = Channel.CreateBounded<PackageItem>(new BoundedChannelOptions(Math.Max(2, report.ActualWorkers * 2)) { FullMode = BoundedChannelFullMode.Wait, SingleWriter = true });
        void Emit(string phase)
        {
            lock (sync) _events?.Invoke(new ExportEvent(1, report.JobId, phase, report.Total, report.Results.Count,
                report.Counts, report.ActualWorkers, timer.Elapsed.TotalMilliseconds));
        }
        void Record(PackageResult result)
        {
            lock (sync)
            {
                manifest.Append(result);
                report.Results.Add(result);
            }
        }
        var formatFingerprint = SafeText.Hash($"{ExportReport.CurrentVersion}|{options.Compact}|{options.KeepStructure}");
        string Fingerprint(PackageItem item) => SafeText.Hash(first.PackageFingerprint(item) + formatFingerprint);
        bool ShouldSkip(PackageItem item, out PackageResult result)
        {
            var target = targets[item.Path];
            var fp = Fingerprint(item);
            result = new PackageResult { Path = item.Path, Fingerprint = fp, State = "skipped-unverified" };
            if (options.Existing == "overwrite" || !File.Exists(target)) return false;
            var info = new FileInfo(target);
            if (info.Length == 0) return false;
            manifest.Records.TryGetValue(item.Path, out var previous);
            if (previous?.State is "failed" or "partial" or "cancelled") return false;
            var matched = previous is { State: "success" or "skipped" } && previous.Fingerprint == fp && previous.Length == info.Length &&
                (previous.Sha256 is null || SafeText.FileHash(target) == previous.Sha256);
            if (options.HashOutputs && previous?.Sha256 is null) matched = false;
            if (options.Existing == "resume" && !matched) return false;
            if (options.VerifyExisting && !OutputSafety.ValidJson(target)) return false;
            result = result with { State = matched ? "skipped" : "skipped-unverified", Length = info.Length, Sha256 = matched ? previous?.Sha256 : null };
            return true;
        }
        var producer = Task.Run(async () =>
        {
            try
            {
                foreach (var item in items)
                {
                    token.ThrowIfCancellationRequested();
                    bool skip;
                    PackageResult result;
                    lock (sync) skip = ShouldSkip(item, out result);
                    if (skip) Record(result);
                    else await pending.Writer.WriteAsync(item, token);
                }
            }
            catch (OperationCanceledException) { }
            catch (Exception e) { fatal.Enqueue(SafeText.Redact(e.Message)); linked.Cancel(); }
            finally { pending.Writer.TryComplete(); }
        }, CancellationToken.None);
        async Task Worker(IExportSession session)
        {
            try
            {
                await foreach (var item in pending.Reader.ReadAllAsync(token))
                {
                    await gate.Enter(item.Size >= 32 * 1024 * 1024, token);
                    try
                    {
                        token.ThrowIfCancellationRequested();
                        var watch = Stopwatch.StartNew();
                        var fp = Fingerprint(item);
                        using var diagnostic = new DiagnosticScope();
                        PackageResult result;
                        try
                        {
                            OutputSafety.Target(output, item.Path, options.KeepStructure);
                            OutputSafety.AtomicWrite(targets[item.Path], w => session.WriteJson(item.Path, w, options.Compact, token), token);
                            var info = new FileInfo(targets[item.Path]);
                            result = new PackageResult
                            {
                                Path = item.Path, Fingerprint = fp, State = diagnostic.Messages.Count > 0 ? "partial" : "success",
                                Length = info.Length, Sha256 = options.HashOutputs ? SafeText.FileHash(info.FullName) : null,
                                Milliseconds = watch.Elapsed.TotalMilliseconds, Diagnostics = diagnostic.Messages.ToArray()
                            };
                        }
                        catch (OperationCanceledException)
                        {
                            result = new PackageResult { Path = item.Path, Fingerprint = fp, State = "cancelled", Milliseconds = watch.Elapsed.TotalMilliseconds };
                        }
                        catch (Exception e)
                        {
                            result = new PackageResult { Path = item.Path, Fingerprint = fp, State = "failed", Milliseconds = watch.Elapsed.TotalMilliseconds,
                                ErrorType = e.GetType().Name, Diagnostics = diagnostic.Messages.Append(SafeText.Redact(e.Message)).ToArray() };
                            if (e is IOException or UnauthorizedAccessException or OutOfMemoryException)
                            { fatal.Enqueue(SafeText.Redact(e.Message)); linked.Cancel(); }
                        }
                        Record(result);
                        session.ClearCache();
                        if (options.FailFast && result.State is "failed" or "partial") { failedFast = true; linked.Cancel(); }
                    }
                    finally { gate.Leave(); }
                }
            }
            catch (OperationCanceledException) { }
            catch (Exception e) { fatal.Enqueue(SafeText.Redact(e.Message)); linked.Cancel(); }
        }
        async Task AdditionalWorker()
        {
            try
            {
                using var session = factory.Create();
                if (session.Fingerprint != first.Fingerprint) throw new InvalidOperationException("Input changed while starting workers.");
                await Worker(session);
            }
            catch (Exception e) { fatal.Enqueue(SafeText.Redact(e.Message)); linked.Cancel(); }
        }
        Emit("started");
        var monitor = Task.Run(async () =>
        {
            try
            {
                while (!token.IsCancellationRequested)
                {
                    var memory = Process.GetCurrentProcess().WorkingSet64;
                    lock (sync) report.PeakMemoryBytes = Math.Max(memory, report.PeakMemoryBytes);
                    if (options.CancelFile is not null && File.Exists(options.CancelFile)) { linked.Cancel(); break; }
                    Emit("exporting");
                    await Task.Delay(1000, token);
                }
            }
            catch (OperationCanceledException) { }
        });
        try
        {
            var workers = new List<Task> { Task.Run(() => Worker(first), CancellationToken.None) };
            for (var i = 1; i < report.ActualWorkers; i++) workers.Add(Task.Run(AdditionalWorker, CancellationToken.None));
            await Task.WhenAll(workers.Append(producer));
        }
        finally
        {
            linked.Cancel();
            await monitor;
            report.ExportMilliseconds = timer.Elapsed.TotalMilliseconds - exportStart;
            report.PeakMemoryBytes = Math.Max(report.PeakMemoryBytes, Process.GetCurrentProcess().WorkingSet64);
            if (report.PeakMemoryBytes > budget) report.Diagnostics.Add("Soft memory budget exceeded; large packages can require an exclusive worker beyond the configured budget.");
            report.Diagnostics.AddRange(jobDiagnostics.Distinct());
            report.Diagnostics.AddRange(fatal.Distinct());
            report.ExitCode = !fatal.IsEmpty ? 4 : cancellationToken.IsCancellationRequested || (options.CancelFile is not null && File.Exists(options.CancelFile)) ? 3 :
                failedFast || !jobDiagnostics.IsEmpty || report.Results.Any(r => r.State is "failed" or "partial") ? 2 : 0;
            try { OutputSafety.AtomicWrite(reportPath, w => w.Write(WireJson.Serialize(report))); }
            catch (Exception e) { report.ExitCode = 4; report.Diagnostics.Add("Could not write report: " + SafeText.Redact(e.Message)); }
            Emit("finished");
        }
        return report;
    }
}

internal sealed class MemoryGate(long budget, int workers)
{
    private readonly object _lock = new();
    private int _active;
    private bool _exclusive;
    public async Task Enter(bool large, CancellationToken token)
    {
        var attemptedGc = false;
        while (true)
        {
            token.ThrowIfCancellationRequested();
            var pressure = Process.GetCurrentProcess().WorkingSet64 >= budget;
            lock (_lock)
            {
                if (!_exclusive && _active < workers && (!large || _active == 0) && (!pressure || _active == 0 && attemptedGc))
                { _active++; _exclusive = large || pressure; return; }
                if (_active == 0 && pressure && !attemptedGc) attemptedGc = true;
                else pressure = false;
            }
            if (pressure) GC.Collect(2, GCCollectionMode.Optimized, true);
            await Task.Delay(25, token);
        }
    }
    public void Leave() { lock (_lock) { _active--; if (_active == 0) _exclusive = false; } }
}
