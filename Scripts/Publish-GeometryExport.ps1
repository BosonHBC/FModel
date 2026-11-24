param(
    [string]$Repository = (Join-Path $PSScriptRoot '..'),
    [string]$Destination = 'I:/PBZ/Releases/fmodel-pbz-geometry',
    [string]$NativeData = 'I:/PBZ/FModelOutputs/.data'
)
$ErrorActionPreference = 'Stop'
$Repository = [IO.Path]::GetFullPath($Repository)
$Destination = [IO.Path]::GetFullPath($Destination)
if ((Test-Path (Join-Path $Destination 'FModel.exe')) -or (Test-Path (Join-Path $Destination 'FModel.Cli.exe'))) { throw 'Destination already has binaries; use a new destination instead of overwriting.' }
foreach ($name in @('oodle-data-shared.dll','zlib-ng2.dll')) {
    if (!(Test-Path (Join-Path $NativeData $name))) { throw "Local native dependency missing: $name" }
}
New-Item -ItemType Directory -Path $Destination -Force | Out-Null
$commit = (& git -C $Repository rev-parse HEAD).Trim()
$branch = (& git -C $Repository branch --show-current).Trim()
$information = "4.4.4.4+$commit-json-export-4-pbz-geometry"
$arguments = @('-c','Release','-r','win-x64','--self-contained','true','-p:PublishSingleFile=true','-p:IncludeNativeLibrariesForSelfExtract=true','-p:CUE4PARSE_SKIP_NATIVE=true','-p:Version=4.4.4.4','-p:FileVersion=4.4.4.4',"-p:InformationalVersion=$information",'-o',$Destination)
& dotnet publish (Join-Path $Repository 'FModel/FModel.csproj') @arguments
if ($LASTEXITCODE -ne 0) { throw 'GUI publish failed' }
& dotnet publish (Join-Path $Repository 'FModel.Cli/FModel.Cli.csproj') @arguments
if ($LASTEXITCODE -ne 0) { throw 'CLI publish failed' }
$data = Join-Path $Destination '.data'
New-Item -ItemType Directory -Path $data -Force | Out-Null
foreach ($name in @('oodle-data-shared.dll','zlib-ng2.dll')) { Copy-Item (Join-Path $NativeData $name) (Join-Path $data $name) }
Copy-Item (Join-Path $Repository 'Docs/json-export.md') (Join-Path $Destination 'README.md')
Copy-Item (Join-Path $Repository 'FModel.Cli/profiles.example.json') (Join-Path $Destination 'profiles.example.json')
Copy-Item (Join-Path $Repository 'LICENSE') (Join-Path $Destination 'LICENSE')
Copy-Item (Join-Path $Repository 'NOTICE') (Join-Path $Destination 'NOTICE')
$hashes = @()
foreach ($name in @('FModel.exe','FModel.Cli.exe','.data/oodle-data-shared.dll','.data/zlib-ng2.dll')) {
    $file = Join-Path $Destination $name
    $hashes += [ordered]@{file=$name; sha256=(Get-FileHash $file -Algorithm SHA256).Hash; bytes=(Get-Item $file).Length}
}
$metadata = [ordered]@{version='4.4.4.4'; toolVersion='4.4.4-json-export-4-pbz-geometry'; baseCommit=$commit; sourceBranch=$branch; sourceHasLocalChanges=$true; platform='win-x64'; selfContained=$true; publishedUtc=[DateTime]::UtcNow.ToString('o'); files=$hashes}
[IO.File]::WriteAllText((Join-Path $Destination 'build-info.json'), ($metadata | ConvertTo-Json -Depth 5), [Text.UTF8Encoding]::new($false))
$cli = Join-Path $Destination 'FModel.Cli.exe'
& $cli --version
if ($LASTEXITCODE -ne 0) { throw 'Published CLI cannot start' }
$help = & $cli --help
if (-not ($help -match 'export-geometry')) { throw 'Published CLI lacks export-geometry command' }
Write-Output "Release written to $Destination. Existing FModel programs were not overwritten."
