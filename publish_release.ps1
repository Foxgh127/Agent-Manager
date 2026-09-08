param(
    [Parameter(Mandatory = $true)][string]$Repository,
    [switch]$Build,
    [switch]$Publish,
    [string]$NotesFile,
    [string]$TargetRef = $env:GITHUB_SHA,
    [string]$OutputDirectory = "release"
)

$ErrorActionPreference = "Stop"
if ($Repository -notmatch '^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$') {
    throw "Repository must be an explicit owner/name."
}
$publishProject = [IO.Path]::GetFullPath($PSScriptRoot)
$releaseDirectory = [IO.Path]::GetFullPath($(if ([IO.Path]::IsPathRooted($OutputDirectory)) { $OutputDirectory } else { Join-Path $publishProject $OutputDirectory }))
if (-not $releaseDirectory.StartsWith($publishProject.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) { throw "Release output must stay inside the project." }
$packageDocument = Get-Content -LiteralPath (Join-Path $publishProject "gui/package.json") -Raw | ConvertFrom-Json
$publishVersion = [string]$packageDocument.version
if ($publishVersion -notmatch '^\d+\.\d+\.\d+$') { throw "This publisher accepts stable x.y.z versions only." }
$publishTag = "v$publishVersion"
$assetName = "AgentManager-$publishVersion.exe"
$sourceDocument = @{kind="github"; repository=$Repository; assetName="AgentManager-{version}.exe"; channel="stable"}
$utf8 = New-Object System.Text.UTF8Encoding($false)

# This is a local, reviewable change. Only the -Publish branch contacts GitHub.
[IO.File]::WriteAllText((Join-Path $publishProject "app-update-source.json"), ($sourceDocument | ConvertTo-Json) + "`n", $utf8)
if ($Build) {
    & (Join-Path $publishProject "build_exe.ps1") -OutputDirectory $OutputDirectory
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
if 'app-update-source.json' not in a.toc:
    raise SystemExit('Update source is not bundled; run publish_release.ps1 with -Build.')
s=json.loads(a.extract('app-update-source.json'))
if s.get('kind')!='github' or s.get('repository')!=sys.argv[2] or s.get('assetName')!='AgentManager-{version}.exe':
    raise SystemExit('Bundled update source differs; rebuild before publishing.')
'@
python -c $verifyCode $builtPath $Repository
if ($LASTEXITCODE -ne 0) { throw "Bundled update source verification failed." }

$publishStage = [IO.Path]::GetFullPath((Join-Path $publishProject "work/publish/$publishVersion"))
if (-not $publishStage.StartsWith($publishProject.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) { throw "Publish staging path escaped the project." }
New-Item -ItemType Directory -Path $publishStage -Force | Out-Null
$assetPath = Join-Path $publishStage $assetName
Copy-Item -LiteralPath $builtPath -Destination $assetPath -Force
$notes = if ($NotesFile) { Get-Content -LiteralPath $NotesFile -Raw } else { "Agent Manager $publishVersion" }
$assetSize = (Get-Item -LiteralPath $assetPath).Length
$releaseManifest = @{schemaVersion=1; appId="openai-agent-manager"; version=$publishVersion; channel="stable";
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
# Never overwrite a public version. Failed validation leaves a draft only.
if (-not $TargetRef) {
    $TargetRef = & git -C $publishProject rev-parse HEAD
    if ($LASTEXITCODE -ne 0) { throw "Commit the release source before publishing." }
}
& gh release create $publishTag $assetPath $shaPath $jsonPath --repo $Repository --target $TargetRef --draft --title "Agent Manager $publishVersion" --notes-file $bodyPath
if ($LASTEXITCODE -ne 0) { throw "Draft release creation/upload failed." }
$remoteText = & gh api "repos/$Repository/releases/tags/$publishTag"
if ($LASTEXITCODE -ne 0) { throw "Could not verify uploaded release; it remains a draft." }
$remote = $remoteText | ConvertFrom-Json
$uploaded = @($remote.assets | Where-Object { $_.name -eq $assetName })
if ($uploaded.Count -ne 1 -or $uploaded[0].size -ne $assetSize -or $uploaded[0].digest -ne "sha256:$expectedHash") {
    throw "Uploaded binary checksum/size did not verify; release remains a draft."
}
& gh release edit $publishTag --repo $Repository --draft=false --latest
if ($LASTEXITCODE -ne 0) { throw "Publish failed; inspect the draft release." }
Write-Host "Published https://github.com/$Repository/releases/tag/$publishTag"
