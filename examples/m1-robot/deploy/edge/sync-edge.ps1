param(
    [switch] $Deploy,
    [switch] $PackageOnly,
    [string] $CloudRepo = (Resolve-Path (Join-Path $PSScriptRoot '../../../..')).Path,
    [string] $Robot,
    [string] $KnownHostsFile
)

$ErrorActionPreference = 'Stop'
$base = $PSScriptRoot
$bundle = Join-Path $base 'runtime'
$work = Join-Path $base '.sync-work'
$stage = Join-Path $work ('stage-' + [guid]::NewGuid().ToString('N'))
$sshOptions = @('-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8')
if ($KnownHostsFile) { $sshOptions += @('-o', "UserKnownHostsFile=$KnownHostsFile") }
if (-not $PackageOnly -and -not $Robot) { throw 'Specify -Robot root@YOUR_ROBOT_IP, or use -PackageOnly.' }
if ($PackageOnly -and $Deploy) { throw '-PackageOnly and -Deploy are mutually exclusive.' }

function Invoke-Checked([string] $program, [string[]] $arguments) {
    & $program @arguments
    if ($LASTEXITCODE -ne 0) { throw "$program failed with exit code $LASTEXITCODE" }
}

function Copy-Tree([string] $relative) {
    $from = Join-Path $CloudRepo $relative
    if (-not (Test-Path -LiteralPath $from)) { throw "Missing cloud source: $relative" }
    $to = Join-Path $stage $relative
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $to) | Out-Null
    Copy-Item -LiteralPath $from -Destination $to -Recurse
}

function Copy-File([string] $from, [string] $relative) {
    if (-not (Test-Path -LiteralPath $from -PathType Leaf)) { throw "Missing source: $from" }
    $to = Join-Path $stage $relative
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $to) | Out-Null
    Copy-Item -LiteralPath $from -Destination $to
}

if (-not (Test-Path -LiteralPath (Join-Path $CloudRepo '.git'))) {
    throw "Cloud repository not found: $CloudRepo"
}
New-Item -ItemType Directory -Force -Path $stage | Out-Null

try {
    foreach ($directory in @('cli/bin', 'cli/src', 'scripts/runtime',
        'server/src', 'shared', 'web/dist')) {
        Copy-Tree $directory
    }
    $configFiles = & git -C $CloudRepo ls-files -- config
    if ($LASTEXITCODE -ne 0) { throw 'Cannot enumerate tracked configuration templates' }
    foreach ($relative in $configFiles) {
        Copy-File (Join-Path $CloudRepo $relative) $relative
    }
    $m1 = Join-Path $CloudRepo 'examples/m1-robot'
    foreach ($file in Get-ChildItem -LiteralPath $m1 -File -Filter '*.mjs') {
        Copy-File $file.FullName "examples/m1-robot/$($file.Name)"
    }
    Copy-File (Join-Path $m1 'm1-robot.config.json') 'examples/m1-robot/m1-robot.config.json'
    Copy-File (Join-Path $m1 'profile/ASSISTANT.md') 'examples/m1-robot/profile/ASSISTANT.md'
    Copy-File (Join-Path $m1 'profile/frontend-profile.json') 'examples/m1-robot/profile/frontend-profile.json'
    Copy-File (Join-Path $m1 'm1-robot.config.edge.json') 'examples/m1-robot/m1-robot.config.edge.json'
    foreach ($relative in @('package.json', 'package-lock.json', 'edge-health.mjs',
        'edge-preflight.mjs', 'verify-release.mjs', 'packages/webrtc/package.json')) {
        Copy-File (Join-Path $bundle $relative) $relative
    }

    $files = @(Get-ChildItem -LiteralPath $stage -File -Recurse | ForEach-Object {
        [pscustomobject]@{
            path = $_.FullName.Substring($stage.Length + 1).Replace('\', '/')
            sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $_.FullName).Hash.ToLowerInvariant()
        }
    } | Sort-Object path)
    $hashLines = ($files | ForEach-Object { "$($_.sha256) $($_.path)`n" }) -join ''
    $tree = [Convert]::ToHexString(
        [Security.Cryptography.SHA256]::HashData([Text.Encoding]::UTF8.GetBytes($hashLines))
    ).ToLowerInvariant()
    $commit = (& git -C $CloudRepo rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0 -or $commit -notmatch '^[0-9a-f]{40}$') { throw 'Cannot read cloud Git commit' }
    $dirtyPaths = & git -C $CloudRepo status --porcelain -- cli config examples/m1-robot packages scripts server shared web/dist
    $dirty = [bool]$dirtyPaths
    if ($LASTEXITCODE -ne 0) { throw 'Cannot read cloud Git status' }
    $release = 'edge-{0}-{1}-{2}' -f (Get-Date -AsUTC -Format 'yyyyMMdd-HHmmss'),
        $commit.Substring(0, 7), $tree.Substring(0, 8)
    $manifest = [ordered]@{
        schema = 'm1-edge-release/v1'
        release = $release
        sourceCommit = $commit
        sourceDirty = $dirty
        sourceTreeSha256 = $tree
        builtAtUtc = (Get-Date).ToUniversalTime().ToString('o')
        files = $files
    }
    $manifest | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $stage 'release-manifest.json') -Encoding utf8
    Invoke-Checked 'node' @((Join-Path $bundle 'verify-release.mjs'), $stage)

    if ($PackageOnly) {
        $archive = Join-Path $work "$release.tar.gz"
        Invoke-Checked 'tar' @('-czf', $archive, '-C', $stage, '.')
        Write-Output "Package: $archive"
        Write-Output "SHA256: $((Get-FileHash -Algorithm SHA256 -LiteralPath $archive).Hash.ToLowerInvariant())"
        return
    }

    $activeName = (& ssh @sshOptions $Robot 'cat /userdata/developer/audio-agent/run/active-release 2>/dev/null || echo uninstalled').Trim()
    if ($LASTEXITCODE -ne 0 -or $activeName -notmatch '^[A-Za-z0-9._-]+$') {
        throw 'Cannot determine robot active release'
    }
    $remoteManifest = (& ssh @sshOptions $Robot "cat /userdata/developer/audio-agent/releases/$activeName/release-manifest.json 2>/dev/null || true") -join "`n"
    if ($LASTEXITCODE -ne 0) { throw 'Cannot read robot release manifest' }
    $remoteVersion = if ($remoteManifest) { $remoteManifest | ConvertFrom-Json } else { $null }
    $remoteTree = $remoteVersion.sourceTreeSha256
    Write-Output "Cloud commit: $commit$(if ($dirty) { ' (working tree modified)' })"
    Write-Output "Local source tree: $tree ($($files.Count) files)"
    Write-Output "Robot active release: $activeName"
    Write-Output "Robot source tree: $(if ($remoteTree) { $remoteTree } else { 'unversioned' })"
    Write-Output "Robot source commit: $(if ($remoteVersion.sourceCommit) { $remoteVersion.sourceCommit } else { 'unversioned' })"
    if ($remoteTree -eq $tree -and $remoteVersion.sourceCommit -eq $commit -and
        $remoteVersion.sourceDirty -eq $dirty) {
        Write-Output 'Source versions are synchronized.'
        return
    }
    if (-not $Deploy) {
        Write-Output 'Source versions differ. Run sync-edge.ps1 -Deploy to publish this snapshot.'
        return
    }

    $archive = Join-Path $work "$release.tar.gz"
    Invoke-Checked 'tar' @('-czf', $archive, '-C', $stage, '.')
    $archiveSha = (Get-FileHash -Algorithm SHA256 -LiteralPath $archive).Hash.ToLowerInvariant()
    Invoke-Checked 'ssh' ($sshOptions + @($Robot,
        'mkdir -p /userdata/developer/audio-agent/run/uploads'))
    Invoke-Checked 'scp' ($sshOptions + @($archive,
        "${Robot}:/userdata/developer/audio-agent/run/uploads/$release.tar.gz"))
    foreach ($script in @('edge-run.sh', 'edge-service.sh', 'edge-supervisor.py', 'install-release.sh', 'activate-release.sh', 'qwen-bare.sh')) {
        Invoke-Checked 'scp' ($sshOptions + @((Join-Path $base $script),
            "${Robot}:/userdata/developer/audio-agent/scripts/$script"))
    }
    Invoke-Checked 'ssh' ($sshOptions + @($Robot,
        "chmod 700 /userdata/developer/audio-agent/scripts/edge-run.sh /userdata/developer/audio-agent/scripts/edge-service.sh /userdata/developer/audio-agent/scripts/qwen-bare.sh /userdata/developer/audio-agent/scripts/install-release.sh /userdata/developer/audio-agent/scripts/activate-release.sh; bash /userdata/developer/audio-agent/scripts/install-release.sh $release $archiveSha"))
    Invoke-Checked 'ssh' ($sshOptions + @($Robot,
        "if test -f /userdata/developer/audio-agent/run/active-release; then bash /userdata/developer/audio-agent/scripts/activate-release.sh $release; else printf '%s\n' $release > /userdata/developer/audio-agent/run/active-release; bash /userdata/developer/audio-agent/scripts/edge-service.sh start; fi"))
    Write-Output "Published and activated: $release"
} finally {
    $rootPath = [IO.Path]::GetFullPath($work).TrimEnd('\') + '\'
    $stagePath = [IO.Path]::GetFullPath($stage)
    if ($stagePath.StartsWith($rootPath, [StringComparison]::OrdinalIgnoreCase) -and
        (Test-Path -LiteralPath $stagePath)) {
        Remove-Item -LiteralPath $stagePath -Recurse -Force
    }
}
