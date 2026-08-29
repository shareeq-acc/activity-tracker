<#
.SYNOPSIS
  Register the collector as a Scheduled Task that starts at logon.

.DESCRIPTION
  Runs the collector with pythonw.exe so there is no console window. The
  ingest token is NOT passed on the command line - the collector reads
  ..\.env itself - so nothing sensitive shows up in the Task Scheduler UI.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\install-startup.ps1
  powershell -ExecutionPolicy Bypass -File .\install-startup.ps1 -Uninstall
#>
param([switch]$Uninstall)

$ErrorActionPreference = 'Stop'
$TaskName = 'ActivityTrackerCollector'

if ($Uninstall) {
    try {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed scheduled task '$TaskName'." -ForegroundColor Green
    } catch {
        Write-Host "No scheduled task named '$TaskName' was registered." -ForegroundColor Yellow
    }
    exit 0
}

$here   = Split-Path -Parent $MyInvocation.MyCommand.Path
$script = Join-Path $here 'collector.py'
$envFile = Join-Path (Split-Path -Parent $here) '.env'

if (-not (Test-Path $script))  { throw "collector.py not found next to this script." }
if (-not (Test-Path $envFile)) { throw "..\.env not found. Copy .env.example to .env first." }

$pythonw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $pythonw) {
    $py = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
    if (-not $py) { throw "Python is not on PATH. Install Python 3.10+ and retry." }
    $pythonw = Join-Path (Split-Path -Parent $py) 'pythonw.exe'
    if (-not (Test-Path $pythonw)) { throw "pythonw.exe not found beside $py" }
}

$action = New-ScheduledTaskAction -Execute $pythonw `
    -Argument "`"$script`"" -WorkingDirectory $here

$trigger = New-ScheduledTaskTrigger -AtLogOn

# Keep running on battery, restart if it ever dies, and never time out.
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -RestartCount 3 `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask -TaskName $TaskName `
    -Action $action -Trigger $trigger -Settings $settings `
    -Description 'Records foreground window activity for the Activity Tracker dashboard.' `
    -Force | Out-Null

Write-Host "Registered '$TaskName' to start at logon." -ForegroundColor Green
Write-Host "Start it now with:  Start-ScheduledTask -TaskName $TaskName"
Write-Host "Stop it with:       Stop-ScheduledTask  -TaskName $TaskName"
Write-Host "Remove it with:     .\install-startup.ps1 -Uninstall"
