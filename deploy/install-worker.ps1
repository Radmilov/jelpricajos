<#
.SYNOPSIS
  Runs this PC as the OCR worker: it watches the stream and pushes results OUT to the
  public relay. Nothing on this PC accepts connections from the internet.

.EXAMPLE
  .\deploy\install-worker.ps1 -RelayUrl https://jelpricajos.example.rs -Token <RELAY_TOKEN>

.EXAMPLE
  .\deploy\install-worker.ps1 -Uninstall

.NOTES
  - Writes local.env (git-ignored) next to app.py: MODE=worker, RELAY_URL, RELAY_TOKEN.
  - Registers a Task Scheduler task for the current user that starts the worker at logon
    (hidden, no console window) and restarts it if it crashes.
  - The local dashboard keeps working on http://127.0.0.1:8000.
#>
param(
  [string] $RelayUrl = "",
  [string] $Token = "",
  [string] $TaskName = "jelpricajos-worker",
  [switch] $Uninstall,
  [switch] $NoStart
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$EnvFile = Join-Path $Root "local.env"

if ($Uninstall) {
  if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed task $TaskName. local.env was left in place." -ForegroundColor Green
  } else {
    Write-Host "Task $TaskName does not exist."
  }
  return
}

if (-not $RelayUrl -or -not $Token) { throw "Both -RelayUrl and -Token are required (the token is printed by the relay installer)." }
$RelayUrl = $RelayUrl.TrimEnd("/")
if ($RelayUrl -notmatch '^https://' -and $RelayUrl -notmatch '^http://(127\.0\.0\.1|localhost)') {
  throw "RelayUrl must start with https:// (the token must not travel in clear text)."
}

$Pythonw = Join-Path $Root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $Pythonw)) { throw "Virtual environment not found: $Pythonw (create it and pip install -r requirements.txt first)." }

# --- local.env: keep unrelated lines, replace the three worker settings -------------
$keep = @()
if (Test-Path $EnvFile) {
  $keep = Get-Content $EnvFile | Where-Object { $_ -notmatch '^\s*(MODE|RELAY_URL|RELAY_TOKEN)\s*=' }
}
$lines = @($keep) + @("MODE=worker", "RELAY_URL=$RelayUrl", "RELAY_TOKEN=$Token")
Set-Content -Path $EnvFile -Value $lines -Encoding utf8
# Only the current user may read the token.
icacls $EnvFile /inheritance:r /grant:r "$($env:USERNAME):(R,W)" | Out-Null
Write-Host "Wrote $EnvFile" -ForegroundColor Cyan

# --- Task Scheduler: start at logon, restart on failure -------------------------
$action = New-ScheduledTaskAction -Execute $Pythonw -Argument "app.py" -WorkingDirectory $Root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
  -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
  -ExecutionTimeLimit (New-TimeSpan -Seconds 0) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
  -Principal $principal -Description "jelpricajos OCR worker (pushes to $RelayUrl)" -Force | Out-Null
Write-Host "Registered task $TaskName (starts at logon, restarts on failure)." -ForegroundColor Cyan

if (-not $NoStart) {
  # Free port 8000 if an older standalone instance is still running.
  $old = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
  foreach ($c in $old) { Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue }
  Start-ScheduledTask -TaskName $TaskName
  Write-Host "Started. Checking in a few seconds..." -ForegroundColor Cyan
  Start-Sleep -Seconds 8
  try {
    $h = Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/health" -TimeoutSec 10
    Write-Host ("Worker mode: {0}; last push to relay: {1}; error: {2}" -f $h.mode, $h.relay.last_push_ok, $h.relay.error)
  } catch {
    Write-Host "The worker is still starting (OCR model load). Check http://127.0.0.1:8000/api/v1/health in a minute."
  }
}
