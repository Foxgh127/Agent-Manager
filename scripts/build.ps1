param(
    [string]$OutputDirectory = "dist",
    [string]$Python = "python"
)
$ErrorActionPreference = "Stop"
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$outputRoot = [IO.Path]::GetFullPath($(if ([IO.Path]::IsPathRooted($OutputDirectory)) { $OutputDirectory } else { Join-Path $projectRoot $OutputDirectory }))
if (-not $outputRoot.StartsWith($projectRoot.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Build output must stay inside this project.' }
foreach ($protected in @('src', 'frontend', 'tests', '.git', 'scripts', 'packaging')) {
    $protectedRoot = Join-Path $projectRoot $protected
    if ($outputRoot -eq $protectedRoot -or $outputRoot.StartsWith($protectedRoot + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Build output overlaps source files.' }
}
$buildRoot = Join-Path $projectRoot 'artifacts/build'
$stageRoot = Join-Path $buildRoot 'staging'
New-Item -ItemType Directory -Path $stageRoot, $outputRoot -Force | Out-Null
Push-Location $projectRoot
try {
    $appVersion = & $Python scripts/sync_version.py
    if ($LASTEXITCODE -ne 0) { throw 'Version synchronization failed.' }
    npm ci --ignore-scripts --prefix frontend
    if ($LASTEXITCODE -ne 0) { throw 'Frontend dependency installation failed.' }
    npm run build --prefix frontend
    if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed.' }
    & $Python scripts/sync_assets.py
    if ($LASTEXITCODE -ne 0) { throw 'Resource staging failed.' }
    $resourceRoot = Join-Path $projectRoot 'src/agent_manager/resources'
    & $Python -m PyInstaller --noconfirm --clean --onefile --windowed --name AgentManager `
        --paths (Join-Path $projectRoot 'src') `
        --runtime-hook (Join-Path $projectRoot 'packaging/runtime_dlls.py') `
        --icon (Join-Path $projectRoot 'packaging/app-icon.ico') `
        --distpath $stageRoot --workpath (Join-Path $buildRoot 'pyinstaller') --specpath (Join-Path $buildRoot 'spec') `
        --version-file (Join-Path $projectRoot 'packaging/version_info.txt') `
        --add-data "$resourceRoot;agent_manager/resources" `
        --collect-all webview --collect-all clr_loader `
        --hidden-import webview.platforms.edgechromium --hidden-import pystray._win32 `
        (Join-Path $projectRoot 'packaging/entry.py')
    if ($LASTEXITCODE -ne 0) { throw 'EXE build failed.' }
    $executable = Join-Path $outputRoot 'AgentManager.exe'
    try { Copy-Item -LiteralPath (Join-Path $stageRoot 'AgentManager.exe') -Destination $executable -Force }
    catch {
        $executable = Join-Path $outputRoot "AgentManager-$appVersion.exe"
        Copy-Item -LiteralPath (Join-Path $stageRoot 'AgentManager.exe') -Destination $executable -Force
        Write-Warning 'The standard EXE is in use; the new version was saved alongside it.'
    }
    $hash = (Get-FileHash -LiteralPath $executable -Algorithm SHA256).Hash.ToLowerInvariant()
    [IO.File]::WriteAllText((Join-Path $outputRoot 'SHA256.txt'), "$hash  $([IO.Path]::GetFileName($executable))`n", (New-Object Text.UTF8Encoding($false)))
    Copy-Item -LiteralPath (Join-Path $projectRoot 'docs/PORTABLE.md') -Destination (Join-Path $outputRoot 'README.txt') -Force
    Write-Host "Built $executable"
    Write-Host "SHA256 $hash"
} finally { Pop-Location }
