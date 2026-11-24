using System.Collections.Concurrent;
using Serilog.Core;
using Serilog.Events;

namespace FModel.Export;

public sealed class DiagnosticScope : IDisposable
{
    private static readonly AsyncLocal<DiagnosticScope?> CurrentPackage = new();
    private static readonly AsyncLocal<ConcurrentQueue<string>?> CurrentTask = new();
    private readonly DiagnosticScope? _previous;
    public List<string> Messages { get; } = [];
    public DiagnosticScope() { _previous = CurrentPackage.Value; CurrentPackage.Value = this; }
    public void Dispose() => CurrentPackage.Value = _previous;
    public static IDisposable BeginTask(ConcurrentQueue<string> diagnostics)
    {
        var previous = CurrentTask.Value;
        CurrentTask.Value = diagnostics;
        return new Restore(() => CurrentTask.Value = previous);
    }
    internal static void Capture(LogEvent e)
    {
        var message = SafeText.Redact(e.RenderMessage() + (e.Exception is null ? "" : " " + e.Exception.Message));
        var warningProblem = e.Level == LogEventLevel.Warning && new[] { "failed", "could not", "missing", "incorrect", "unexpected", "unhandled" }
            .Any(word => message.Contains(word, StringComparison.OrdinalIgnoreCase));
        if (e.Level < LogEventLevel.Error && !warningProblem) return;
        var package = CurrentPackage.Value;
        if (package is not null) { lock (package.Messages) package.Messages.Add(message); }
        else CurrentTask.Value?.Enqueue(message);
    }
    private sealed class Restore(Action restore) : IDisposable { public void Dispose() => restore(); }
}
public sealed class DiagnosticSink : ILogEventSink
{
    public static readonly DiagnosticSink Instance = new();
    public void Emit(LogEvent logEvent) => DiagnosticScope.Capture(logEvent);
}
public sealed class RedactedConsoleSink : ILogEventSink
{
    public void Emit(LogEvent logEvent)
    {
        if (logEvent.Level >= LogEventLevel.Warning)
            Console.Error.WriteLine(SafeText.Redact($"[{logEvent.Level}] {logEvent.RenderMessage()} {logEvent.Exception?.Message}"));
    }
}
