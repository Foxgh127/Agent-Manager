param([switch]$IncludeDependencies, [switch]$LegacyRelease)
$ErrorActionPreference='Stop'
$entry=Join-Path $PSScriptRoot 'cleanup_project.py'
$cleanupArgs=@($entry,'--apply')
if ($IncludeDependencies) { $cleanupArgs+='--dependencies' }
if ($LegacyRelease) { $cleanupArgs+='--legacy-release' }
& python @cleanupArgs
if ($LASTEXITCODE -ne 0) { throw 'Cleanup did not complete.' }
