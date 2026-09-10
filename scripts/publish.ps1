param(
    [Parameter(Mandatory = $true)][string]$Repository,
    [switch]$Build,
    [switch]$Publish,
    [string]$NotesFile,
    [string]$TargetRef = $env:GITHUB_SHA,
    [string]$OutputDirectory = "dist"
)

$ErrorActionPreference = "Stop"
if ($Repository -notmatch '^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$') {
    throw "Repository must be an explicit owner/name."
}
$publishProject = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$releaseDirectory = [IO.Path]::GetFullPath($(if ([IO.Path]::IsPathRooted($OutputDirectory)) { $OutputDirectory } else { Join-Path $publishProject $OutputDirectory }))
if (-not $releaseDirectory.StartsWith($publishProject.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) { throw "Release output must stay inside the project." }
& python (Join-Path $publishProject "scripts/sync_version.py")
if ($LASTEXITCODE -ne 0) { throw "Version synchronization failed." }
$releaseEpoch = (Get-Content -LiteralPath (Join-Path $publishProject "src/agent_manager/resources/version.json") -Raw | ConvertFrom-Json).releaseEpoch
$packageDocument = Get-Content -LiteralPath (Join-Path $publishProject "frontend/package.json") -Raw | ConvertFrom-Json
$publishVersion = [string]$packageDocument.version
if ($publishVersion -notmatch '^\d+\.\d+\.\d+$') { throw "This publisher accepts stable x.y.z versions only." }
$publishTag = "v$publishVersion"
$assetName = "AgentManager-$publishVersion.exe"
$sourceDocument = @{kind="github"; repository=$Repository; assetName="AgentManager-{version}.exe"; channel="stable"}
$utf8 = New-Object System.Text.UTF8Encoding($false)

# This is a local, reviewable change. Only the -Publish branch contacts GitHub.
[IO.File]::WriteAllText((Join-Path $publishProject "src/agent_manager/resources/app-update-source.json"), ($sourceDocument | ConvertTo-Json) + "`n", $utf8)
if ($Build) {
    & (Join-Path $publishProject "scripts/build.ps1") -OutputDirectory $OutputDirectory
    if (-not $?) { throw "Build failed." }
}

$manifestLine = (Get-Content -LiteralPath (Join-Path $releaseDirectory "SHA256.txt") -TotalCount 1).Trim()
if ($manifestLine -notmatch '^([A-Fa-f0-9]{64})\s+([A-Za-z0-9_.-]+\.exe)$') { throw "Release SHA256.txt is invalid." }
$expectedHash = $Matches[1].ToLowerInvariant()
$builtName = $Matches[2]
$builtPath = [IO.Path]::GetFullPath((Join-Path $releaseDirectory $builtName))
if (-not $builtPath.StartsWith($releaseDirectory.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) { throw "Release path escaped its directory." }
if ((Get-FileHash -LiteralPath $builtPath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expectedHash) { throw "Release executable does not match SHA256.txt." }
$builtVersion = (Get-Item -LiteralPath $builtPath).VersionInfo.FileVersion
if ($builtVersion -ne $publishVersion) { throw "Executable version differs from source version; run with -Build." }

# A public build must contain the same feed the publisher is about to use.
$verifyCode = @'
import json,sys
from PyInstaller.archive.readers import CArchiveReader
a=CArchiveReader(sys.argv[1])
name=next((key for key in a.toc if key.replace(chr(92),'/')=='agent_manager/resources/app-update-source.json'),None)
if name is None:
    raise SystemExit('Update source is not bundled; run scripts/publish.ps1 with -Build.')
s=json.loads(a.extract(name))
if s.get('kind')!='github' or s.get('repository')!=sys.argv[2] or s.get('assetName')!='AgentManager-{version}.exe':
    raise SystemExit('Bundled update source differs; rebuild before publishing.')
'@
python -c $verifyCode $builtPath $Repository
if ($LASTEXITCODE -ne 0) { throw "Bundled update source verification failed." }
& python (Join-Path $publishProject "scripts/verify_package.py") $builtPath
if ($LASTEXITCODE -ne 0) { throw "Relocated package startup verification failed." }

$publishStage = [IO.Path]::GetFullPath((Join-Path $publishProject "artifacts/publish/$publishVersion"))
if (-not $publishStage.StartsWith($publishProject.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) { throw "Publish staging path escaped the project." }
New-Item -ItemType Directory -Path $publishStage -Force | Out-Null
$assetPath = Join-Path $publishStage $assetName
Copy-Item -LiteralPath $builtPath -Destination $assetPath -Force
$bodyPath = Join-Path $publishStage "release-notes.md"
if ($NotesFile) {
    & python (Join-Path $publishProject 'scripts/release_notes.py') --version $publishVersion --input $NotesFile --output $bodyPath
    if ($LASTEXITCODE -ne 0) { throw 'Release notes do not match the requested version.' }
    $notes = Get-Content -LiteralPath $bodyPath -Raw -Encoding UTF8
} else { $notes = "Agent Manager $publishVersion" }
$assetSize = (Get-Item -LiteralPath $assetPath).Length
$releaseManifest = @{schemaVersion=1; appId="openai-agent-manager"; version=$publishVersion; channel="stable"; releaseEpoch=$releaseEpoch;
    publishedAt=[DateTime]::UtcNow.ToString("o"); releaseNotes=$notes;
    assets=@(@{platform="windows-x64"; name=$assetName; url="https://github.com/$Repository/releases/download/$publishTag/$assetName"; size=$assetSize; sha256=$expectedHash})}
$jsonPath = Join-Path $publishStage "app-update-manifest.json"
$shaPath = Join-Path $publishStage "SHA256.txt"
$bodyPath = Join-Path $publishStage "release-notes.md"
[IO.File]::WriteAllText($jsonPath, ($releaseManifest | ConvertTo-Json -Depth 5) + "`n", $utf8)
[IO.File]::WriteAllText($shaPath, "$expectedHash  $assetName`n", $utf8)
[IO.File]::WriteAllText($bodyPath, $notes, $utf8)
Write-Host "Prepared local release: $publishStage"
if (-not $Publish) { Write-Host "Nothing uploaded. Review these files, then add -Publish."; exit 0 }

$privacy = & gh api "repos/$Repository" --jq '.private'
if ($LASTEXITCODE -ne 0 -or $privacy.Trim() -ne "false") { throw "An existing public release repository is required." }
# Public versions are immutable. A failed draft can be rebuilt and retried.
if (-not $TargetRef) {
    $TargetRef = & git -C $publishProject rev-parse HEAD
    if ($LASTEXITCODE -ne 0) { throw "Commit the release source before publishing." }
}
$releaseList = & gh api "repos/$Repository/releases?per_page=100"
if ($LASTEXITCODE -ne 0) { throw "Release lookup failed." }
$existing = @(($releaseList | ConvertFrom-Json) | Where-Object { $_.tag_name -eq $publishTag })
if ($existing.Count -gt 1) { throw "Ambiguous release version." }
if ($existing.Count -eq 1) {
    if (-not $existing[0].draft) { throw "This version is already public; increase the version number." }
    & gh release upload $publishTag $assetPath $shaPath $jsonPath --repo $Repository --clobber
    if ($LASTEXITCODE -ne 0) { throw "Draft asset upload failed." }
    & gh release edit $publishTag --repo $Repository --target $TargetRef --title "Agent Manager $publishVersion" --notes-file $bodyPath
    if ($LASTEXITCODE -ne 0) { throw "Draft metadata update failed." }
    $releaseId = $existing[0].id
} else {
    & gh release create $publishTag $assetPath $shaPath $jsonPath --repo $Repository --target $TargetRef --draft --title "Agent Manager $publishVersion" --notes-file $bodyPath
    if ($LASTEXITCODE -ne 0) { throw "Draft release creation/upload failed." }
    $releaseId = & gh release view $publishTag --repo $Repository --json databaseId --jq '.databaseId'
    if ($LASTEXITCODE -ne 0) { throw "Could not resolve the draft release ID." }
}
# GitHub does not create the git tag for a new draft until publication; the
# /releases/tags endpoint therefore cannot be used to verify draft uploads.
$remoteText = & gh api "repos/$Repository/releases/$releaseId"
if ($LASTEXITCODE -ne 0) { throw "Could not verify uploaded release; it remains a draft." }
$remote = $remoteText | ConvertFrom-Json
$uploaded = @($remote.assets | Where-Object { $_.name -eq $assetName })
if ($uploaded.Count -ne 1 -or $uploaded[0].size -ne $assetSize -or $uploaded[0].digest -ne "sha256:$expectedHash") {
    throw "Uploaded binary checksum/size did not verify; release remains a draft."
}
& gh release edit $publishTag --repo $Repository --draft=false --latest
if ($LASTEXITCODE -ne 0) { throw "Publish failed; inspect the draft release." }
Write-Host "Published https://github.com/$Repository/releases/tag/$publishTag"
