param([switch]$IncludeDependencies, [switch]$LegacyRelease, [switch]$ReportsOnly, [switch]$Preview)
$ErrorActionPreference='Stop'
$entry=Join-Path $PSScriptRoot 'cleanup_project.py'
$cleanupArgs=@($entry)
if (-not $Preview) { $cleanupArgs+='--apply' }
if ($IncludeDependencies) { $cleanupArgs+='--dependencies' }
if ($LegacyRelease) { $cleanupArgs+='--legacy-release' }
if ($ReportsOnly) { $cleanupArgs+='--reports-only' }
& python @cleanupArgs
if ($LASTEXITCODE -ne 0) { throw 'Cleanup did not complete.' }
