<#
.SYNOPSIS
  Deploys (or updates) the Vučić monitor on a Linux VPS from this Windows machine.

.EXAMPLE
  .\deploy\upload.ps1 -Server ubuntu@203.0.113.10 -Key $HOME\.ssh\oracle.key

.EXAMPLE
  # with a permanent Cloudflare tunnel (token from Zero Trust > Networks > Tunnels)
  .\deploy\upload.ps1 -Server ubuntu@203.0.113.10 -Key $HOME\.ssh\oracle.key -TunnelToken eyJh...

.NOTES
  Re-run any time to push code updates. The server keeps its own database after the first
  install; local speech history is only imported if the server has none yet.
#>
param(
  [Parameter(Mandatory = $true)] [string] $Server,   # user@host
  [string] $Key = "",                                # path to SSH private key (optional)
  [string] $TunnelToken = "",                        # Cloudflare tunnel token (optional)
  [string] $Cookies = "",                            # path to a YouTube cookies.txt (optional)
  [switch] $NoData                                   # don't send the local speech database
)
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$Stage = Join-Path $env:TEMP ("vucic-deploy-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
New-Item -ItemType Directory -Path $Stage | Out-Null

# Use the Windows-bundled tools explicitly (Git for Windows ships incompatible tar/ssh builds).
function Find-Tool($name, $candidates) {
  foreach ($c in $candidates) { if (Test-Path $c) { return $c } }
  $cmd = Get-Command $name -ErrorAction SilentlyContinue
  if ($cmd) { return $cmd.Source }
  throw "$name not found (Windows 10 1803+ includes it; enable 'OpenSSH Client' in Optional Features)"
}
$Tar = Find-Tool "tar.exe" @("$env:SystemRoot\System32\tar.exe")
$Ssh = Find-Tool "ssh.exe" @("$env:SystemRoot\System32\OpenSSH\ssh.exe")
$Scp = Find-Tool "scp.exe" @("$env:SystemRoot\System32\OpenSSH\scp.exe")

$sshArgs = @("-o", "StrictHostKeyChecking=accept-new")
if ($Key) { $sshArgs += @("-i", $Key) }

try {
  Write-Host "==> Packing project" -ForegroundColor Cyan
  $tgz = Join-Path $Stage "app.tgz"
  & $Tar -czf $tgz `
    --exclude=.venv --exclude=data --exclude=__pycache__ --exclude=.pytest_cache `
    --exclude=extension --exclude=tests --exclude=*.pyc --exclude=cookies.txt `
    -C $Root .
  if ($LASTEXITCODE -ne 0) { throw "tar failed" }
  $files = @($tgz)

  if (-not $NoData -and (Test-Path (Join-Path $Root "data\speeches.db"))) {
    Write-Host "==> Snapshotting speech database" -ForegroundColor Cyan
    $snap = Join-Path $Stage "speeches.db"
    $py = Join-Path $Root ".venv\Scripts\python.exe"
    if (-not (Test-Path $py)) { $py = "python" }
    # SQLite online backup: consistent copy even while the local app is running.
    & $py -c "import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()" `
      (Join-Path $Root "data\speeches.db") $snap
    if ($LASTEXITCODE -ne 0) { throw "database snapshot failed" }
    $files += $snap
  }
  if ($Cookies) {
    if (-not (Test-Path $Cookies)) { throw "cookies file not found: $Cookies" }
    Copy-Item $Cookies (Join-Path $Stage "cookies.txt")
    $files += (Join-Path $Stage "cookies.txt")
  }

  Write-Host "==> Uploading to $Server" -ForegroundColor Cyan
  & $Ssh @sshArgs $Server "rm -rf ~/vucic-upload && mkdir -p ~/vucic-upload"
  if ($LASTEXITCODE -ne 0) { throw "ssh connection failed" }
  & $Scp @sshArgs @files "${Server}:~/vucic-upload/"
  if ($LASTEXITCODE -ne 0) { throw "upload failed" }

  Write-Host "==> Installing on the server (first run takes 5-15 minutes)" -ForegroundColor Cyan
  $envPrefix = ""
  if ($TunnelToken) { $envPrefix = "CF_TUNNEL_TOKEN='$TunnelToken' " }
  $remote = @(
    "set -e",
    "cd ~/vucic-upload",
    "rm -rf src && mkdir src && tar -xzf app.tgz -C src",
    "mkdir -p src/data",
    "if [ -f speeches.db ]; then mv speeches.db src/data/; fi",
    "if [ -f cookies.txt ]; then mv cookies.txt src/; fi",
    # Windows line endings would break bash/systemd files
    "sed -i 's/\r$//' src/deploy/*.sh src/deploy/*.service src/deploy/*.example",
    "sudo ${envPrefix}bash src/deploy/install.sh"
  ) -join " && "
  & $Ssh -t @sshArgs $Server $remote
  if ($LASTEXITCODE -ne 0) { throw "remote install failed (exit $LASTEXITCODE)" }

  Write-Host ""
  Write-Host "Deployed. Check status any time with:" -ForegroundColor Green
  Write-Host "  ssh $($sshArgs -join ' ') $Server sudo bash /opt/vucic-monitor/deploy/status.sh"
}
finally {
  Remove-Item -Recurse -Force $Stage -ErrorAction SilentlyContinue
}
