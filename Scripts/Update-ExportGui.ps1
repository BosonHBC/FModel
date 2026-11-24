$ErrorActionPreference = 'Stop'
function Edit($path, $old, $new) {
    $text = [IO.File]::ReadAllText($path)
    if (!$text.Contains($old)) { throw "Source anchor not found: $path" }
    [IO.File]::WriteAllText($path, $text.Replace($old, $new))
}
Edit 'I:/FModel/FModel/Settings/UserSettings.cs' 'public static UserSettings Default { get; set; }' @'
public static UserSettings Default { get; set; }
    public int JsonExportWorkers { get; set; } = 2;
    public int JsonExportMemoryMb { get; set; } = 4096;
    public string JsonExportExisting { get; set; } = "skip";
    public bool JsonExportCompact { get; set; }
'@
Edit 'I:/FModel/FModel/ViewModels/Commands/RightClickMenuCommand.cs' 'Interlocked.Exchange(ref contextViewModel.CUE4Parse.ExportedCount, 0);' @'
if (bulktype == EBulkType.Properties && (folders.Length > 0 || assets.Length > 1))
        {
            await BulkJsonExport.Execute(contextViewModel, folders, assets);
            return;
        }

        Interlocked.Exchange(ref contextViewModel.CUE4Parse.ExportedCount, 0);
'@
Edit 'I:/FModel/FModel/App.xaml.cs' '.WriteTo.Sink(ImGuiSink.Instance)' @'
.WriteTo.Sink(ImGuiSink.Instance)
            .WriteTo.Sink(FModel.Export.DiagnosticSink.Instance)
'@
Edit 'I:/FModel/FModel/Views/SettingsView.xaml' '<DataTemplate x:Key="GeneralTemplate">' @'
<DataTemplate x:Key="JsonExportTemplate">
                <StackPanel Margin="12">
                    <TextBlock Text="Batch JSON Export" FontSize="18" Margin="0 0 0 16" />
                    <TextBlock Text="Existing files" Margin="0 0 0 4" />
                    <ComboBox SelectedValuePath="Content" SelectedValue="{Binding JsonExportExisting, Source={x:Static local:Settings.UserSettings.Default}, Mode=TwoWay}" Margin="0 0 0 12">
                        <ComboBoxItem Content="skip" />
                        <ComboBoxItem Content="resume" />
                        <ComboBoxItem Content="overwrite" />
                    </ComboBox>
                    <TextBlock Text="Workers (1-32; unvalidated presets fall back to 1)" Margin="0 0 0 4" />
                    <TextBox Text="{Binding JsonExportWorkers, Source={x:Static local:Settings.UserSettings.Default}, Mode=TwoWay, UpdateSourceTrigger=PropertyChanged}" Margin="0 0 0 12" />
                    <TextBlock Text="Soft memory budget (MiB, minimum 256)" Margin="0 0 0 4" />
                    <TextBox Text="{Binding JsonExportMemoryMb, Source={x:Static local:Settings.UserSettings.Default}, Mode=TwoWay, UpdateSourceTrigger=PropertyChanged}" Margin="0 0 0 12" />
                    <CheckBox Content="Compact JSON (same properties, fewer whitespace characters)" IsChecked="{Binding JsonExportCompact, Source={x:Static local:Settings.UserSettings.Default}, Mode=TwoWay}" Margin="0 0 0 12" />
                    <TextBlock TextWrapping="Wrap" Text="skip preserves existing nonempty files before parsing; legacy files are marked skipped-unverified. resume requires matching successful records. overwrite explicitly replaces selected outputs after a successful write. Progress and reports are available in the log and output/.fmodel-export folder." />
                </StackPanel>
            </DataTemplate>
            <DataTemplate x:Key="GeneralTemplate">
'@
Edit 'I:/FModel/FModel/Views/SettingsView.xaml' '<TreeViewItem Tag="ThemesTemplate">' @'
<TreeViewItem Tag="JsonExportTemplate" Header="Batch JSON Export" />
            <TreeViewItem Tag="ThemesTemplate">
'@
