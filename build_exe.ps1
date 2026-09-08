param(
    [switch]$CleanupOnly,
    [string]$OutputDirectory = "release"
)

$ErrorActionPreference = "Stop"
if (Get-Variable -Name PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue) {
    $PSNativeCommandUseErrorActionPreference = $true
}

$projectRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
$outputRoot = [System.IO.Path]::GetFullPath($(if ([IO.Path]::IsPathRooted($OutputDirectory)) { $OutputDirectory } else { Join-Path $projectRoot $OutputDirectory }))
$workRoot = [System.IO.Path]::GetFullPath((Join-Path $projectRoot "work\pyinstaller"))
$specRoot = [System.IO.Path]::GetFullPath((Join-Path $projectRoot "work"))
$stageRoot = [System.IO.Path]::GetFullPath((Join-Path $projectRoot "work\release-staging"))
$appVersion = (Get-Content -LiteralPath (Join-Path $projectRoot "gui\package.json") -Raw | ConvertFrom-Json).version

function Test-PathInsideProject([string]$Candidate) {
    $root = $projectRoot.TrimEnd([System.IO.Path]::DirectorySeparatorChar, [System.IO.Path]::AltDirectorySeparatorChar)
    return $Candidate.Equals($root, [System.StringComparison]::OrdinalIgnoreCase) -or
        $Candidate.StartsWith($root + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)
}

if (-not (Test-PathInsideProject $outputRoot) -or -not (Test-PathInsideProject $workRoot) -or -not (Test-PathInsideProject $stageRoot)) {
    throw "Build paths must stay inside the project directory."
}

if ($CleanupOnly) {
    if (-not (Test-Path -LiteralPath $outputRoot)) {
        throw "Release directory does not exist: $outputRoot"
    }
    Get-ChildItem -LiteralPath $outputRoot -Filter "AgentManager-*.exe" -File -ErrorAction SilentlyContinue |
        ForEach-Object {
            $stalePath = $_.FullName
            try {
                Remove-Item -LiteralPath $stalePath -Force -ErrorAction Stop
            } catch {
                Write-Warning "Skipped running stale executable: $stalePath"
            }
        }
    Get-ChildItem -LiteralPath $outputRoot -Filter "CodexAgentManager*.exe" -File -ErrorAction SilentlyContinue |
        ForEach-Object {
            $legacyPath = $_.FullName
            try {
                Remove-Item -LiteralPath $legacyPath -Force -ErrorAction Stop
            } catch {
                Write-Warning "Skipped running legacy executable: $legacyPath"
            }
        }
    $canonical = Join-Path $outputRoot "AgentManager.exe"
    if (-not (Test-Path -LiteralPath $canonical -PathType Leaf)) {
        throw "Canonical release executable is missing: $canonical"
    }
    $remainingExecutables = Get-ChildItem -LiteralPath $outputRoot -Filter "*.exe" -File -ErrorAction SilentlyContinue |
        Where-Object { -not $_.FullName.Equals($canonical, [System.StringComparison]::OrdinalIgnoreCase) }
    if ($remainingExecutables) {
        $names = ($remainingExecutables | ForEach-Object { $_.Name }) -join ", "
        throw "Release cleanup could not remove unmanifested executable(s): $names. Close every old Agent Manager process and retry."
    }
    $hash = (Get-FileHash -LiteralPath $canonical -Algorithm SHA256).Hash
    Set-Content -LiteralPath (Join-Path $outputRoot "SHA256.txt") -Value "$hash  AgentManager.exe" -Encoding ascii
    foreach ($cleanupPath in @(
        $workRoot,
        $stageRoot,
        (Join-Path $specRoot "AgentManager.spec"),
        (Join-Path $projectRoot "gui\work"),
        (Join-Path $projectRoot "gui\node_modules"),
        (Join-Path $projectRoot "__pycache__"),
        (Join-Path $projectRoot ".pytest_cache")
    )) {
        $resolvedCleanup = [System.IO.Path]::GetFullPath($cleanupPath)
        if (-not (Test-PathInsideProject $resolvedCleanup)) {
            throw "Cleanup path escaped the project directory: $resolvedCleanup"
        }
        if (Test-Path -LiteralPath $resolvedCleanup) {
            Remove-Item -LiteralPath $resolvedCleanup -Recurse -Force
        }
    }
    Write-Host "Release cleanup complete: $canonical"
    Write-Host "SHA256: $hash"
    exit 0
}

New-Item -ItemType Directory -Path $outputRoot -Force | Out-Null
New-Item -ItemType Directory -Path $stageRoot -Force | Out-Null

Push-Location (Join-Path $projectRoot "gui")
try {
    # The React/Vite dependency graph does not require install-time lifecycle
    # scripts.  Refuse to execute transitive package scripts while restoring
    # the exact lockfile graph; the production build itself runs explicitly
    # in the next step.
    npm ci --ignore-scripts
    if ($LASTEXITCODE -ne 0) {
        throw "npm ci failed with exit code $LASTEXITCODE."
    }
    $distRoot = Join-Path $projectRoot "gui\dist"
    if (Test-Path -LiteralPath $distRoot) {
        Remove-Item -LiteralPath $distRoot -Recurse -Force
    }
    npm run build
    if ($LASTEXITCODE -ne 0) {
        throw "Frontend build failed with exit code $LASTEXITCODE."
    }
    $distIndex = Join-Path $distRoot "index.html"
    $distAssets = Join-Path $distRoot "assets"
    if (-not (Test-Path -LiteralPath $distIndex -PathType Leaf) -or
        -not (Test-Path -LiteralPath $distAssets -PathType Container) -or
        -not (Get-ChildItem -LiteralPath $distAssets -File -ErrorAction SilentlyContinue | Select-Object -First 1)) {
        throw "Frontend build did not produce a complete gui\dist payload."
    }
} finally {
    Pop-Location
}

$appUpdateBundleArgs = @()
$appUpdateSourcePath = Join-Path $projectRoot "app-update-source.json"
if (Test-Path -LiteralPath $appUpdateSourcePath -PathType Leaf) {
    python -c 'import json,sys; from app_update_service import validate_source; validate_source(json.load(open(sys.argv[1], encoding="utf-8-sig")))' $appUpdateSourcePath
    if ($LASTEXITCODE -ne 0) { throw "Bundled update source is invalid." }
    $appUpdateBundleArgs = @("--add-data", "$appUpdateSourcePath;.")
}

python -m PyInstaller @appUpdateBundleArgs `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --runtime-hook (Join-Path $projectRoot "pyi_rth_agent_manager_dlls.py") `
    --name AgentManager `
    --icon (Join-Path $projectRoot "assets\app-icon.ico") `
    --distpath $stageRoot `
    --workpath $workRoot `
    --specpath $specRoot `
    --version-file (Join-Path $projectRoot "version_info.txt") `
    --add-data "$(Join-Path $projectRoot 'gui\dist');gui\dist" `
    --add-data "$(Join-Path $projectRoot 'assets');assets" `
    --collect-all webview `
    --collect-all clr_loader `
    --hidden-import webview.platforms.edgechromium `
    --hidden-import pystray._win32 `
    (Join-Path $projectRoot "agent_manager_app.py")

if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE."
}

$stagedExecutable = Join-Path $stageRoot "AgentManager.exe"
$canonicalExecutable = Join-Path $outputRoot "AgentManager.exe"
$versionedExecutable = Join-Path $outputRoot "AgentManager-$appVersion.exe"
$executablePath = $canonicalExecutable
$lockedCanonicalHandoff = $false
try {
    Copy-Item -LiteralPath $stagedExecutable -Destination $canonicalExecutable -Force -ErrorAction Stop
} catch {
    $lockedCanonicalHandoff = $true
    $executablePath = $versionedExecutable
    try {
        Copy-Item -LiteralPath $stagedExecutable -Destination $versionedExecutable -Force -ErrorAction Stop
    } catch {
        $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
        $executablePath = Join-Path $outputRoot "AgentManager-$appVersion-$stamp.exe"
        Copy-Item -LiteralPath $stagedExecutable -Destination $executablePath -Force
    }
    Write-Warning "The canonical EXE is currently running. Built the versioned executable instead: $executablePath"
}
Copy-Item -LiteralPath (Join-Path $projectRoot "PORTABLE.txt") -Destination (Join-Path $outputRoot "PORTABLE.txt") -Force

# Keep only the current distributable. Versioned executables are temporary
# fallbacks used when the canonical EXE is locked during an upgrade; older
# copies are never part of the final release directory.
$staleExecutables = Get-ChildItem -LiteralPath $outputRoot -Filter "AgentManager-*.exe" -File -ErrorAction SilentlyContinue |
    Where-Object { $_.FullName -ne $executablePath }
foreach ($staleExecutable in $staleExecutables) {
    try {
        Remove-Item -LiteralPath $staleExecutable.FullName -Force -ErrorAction Stop
    } catch {
        # A running one-file PyInstaller executable stays locked on Windows.
        # Do not turn an otherwise valid release into a failed build; the next
        # build removes the stale copy after that process has exited.
        Write-Warning "Skipped running stale executable: $($staleExecutable.FullName)"
    }
}

# The product was renamed in 5.7.0.  Remove legacy-named binaries after the
# new AgentManager executable is safely staged. A currently running legacy
# copy can remain locked; the next build removes it once that process exits.
$legacyExecutables = Get-ChildItem -LiteralPath $outputRoot -Filter "CodexAgentManager*.exe" -File -ErrorAction SilentlyContinue
foreach ($legacyExecutable in $legacyExecutables) {
    try {
        Remove-Item -LiteralPath $legacyExecutable.FullName -Force -ErrorAction Stop
    } catch {
        Write-Warning "Skipped running legacy executable: $($legacyExecutable.FullName)"
    }
}

# A normally completed release contains one executable.  The only temporary
# exception is an in-place update while the canonical executable is running:
# keep that exact locked source beside one versioned handoff target.  The
# manager's quick-restart path selects the newer versioned target, promotes its
# exact bytes to AgentManager.exe after the old process releases the lock, then
# removes the fallback on the next canonical start.  Any third executable still
# fails closed so the handoff can never choose an ambiguous target.
$unmanifestedExecutables = Get-ChildItem -LiteralPath $outputRoot -Filter "*.exe" -File -ErrorAction SilentlyContinue |
    Where-Object {
        $_.FullName -ne $executablePath -and
        -not (
            $lockedCanonicalHandoff -and
            $_.FullName.Equals($canonicalExecutable, [System.StringComparison]::OrdinalIgnoreCase)
        )
    }
if ($unmanifestedExecutables) {
    $names = ($unmanifestedExecutables | ForEach-Object { $_.Name }) -join ", "
    throw "Release directory still contains unmanifested executable(s): $names. Close every old Agent Manager process and rebuild."
}

$sha256 = [System.Security.Cryptography.SHA256]::Create()
try {
    $hashStream = [System.IO.File]::OpenRead($executablePath)
    try {
        $hashBytes = $sha256.ComputeHash($hashStream)
    } finally {
        $hashStream.Dispose()
    }
    $executableHash = ([System.BitConverter]::ToString($hashBytes)).Replace("-", "")
} finally {
    $sha256.Dispose()
}
Set-Content -LiteralPath (Join-Path $outputRoot "SHA256.txt") -Value "$executableHash  $([System.IO.Path]::GetFileName($executablePath))" -Encoding ascii
if ($lockedCanonicalHandoff) {
    Write-Warning "Built a safe running-update handoff. Use Agent Manager's quick restart to promote: $executablePath"
}

# PyInstaller staging content and the generated spec are reproducible. Remove
# them after a successful build so repeated releases do not accumulate stale
# binaries or module graphs.
foreach ($cleanupPath in @(
    $workRoot,
    $stageRoot,
    (Join-Path $specRoot "AgentManager.spec"),
    (Join-Path $projectRoot "gui\work"),
    (Join-Path $projectRoot "gui\node_modules"),
    (Join-Path $projectRoot ".ruff_cache"),
    (Join-Path $projectRoot "gui\.ruff_cache"),
    (Join-Path $projectRoot "__pycache__")
)) {
    $resolvedCleanup = [System.IO.Path]::GetFullPath($cleanupPath)
    if (-not (Test-PathInsideProject $resolvedCleanup)) {
        throw "Cleanup path escaped the project directory: $resolvedCleanup"
    }
    if (Test-Path -LiteralPath $resolvedCleanup) {
        Remove-Item -LiteralPath $resolvedCleanup -Recurse -Force
    }
}

Write-Host "Built: $executablePath"
Write-Host "SHA256: $executableHash"
