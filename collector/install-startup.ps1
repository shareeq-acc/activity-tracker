<#
.SYNOPSIS
  Make the collector start automatically at logon.

.DESCRIPTION
  Tries a Scheduled Task first, which is the sturdier option: it can restart
  the collector if it dies. That needs administrator rights, so when they are
  not available this falls back to a per-user Startup-folder entry, which
  needs no elevation and works just as well for staying running.

  Either way the collector runs via pythonw.exe, so there is no console
  window, and it reads ..\.env itself, so the ingest token never appears in
  a command line or in the Task Scheduler UI.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\install-startup.ps1
  powershell -ExecutionPolicy Bypass -File .\install-startup.ps1 -Uninstall
#>
param([switch]$Uninstall)

$ErrorActionPreference = 'Stop'
$TaskName   = 'ActivityTrackerCollector'
$here       = Split-Path -Parent $MyInvocation.MyCommand.Path
$script     = Join-Path $here 'collector.py'
$envFile    = Join-Path (Split-Path -Parent $here) '.env'
$startupDir = [Environment]::GetFolderPath('Startup')
$vbsPath    = Join-Path $startupDir 'ActivityTrackerCollector.vbs'

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    (New-Object Security.Principal.WindowsPrincipal $id).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

# --------------------------------------------------------------- uninstall
if ($Uninstall) {
    $removed = $false
    try {
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        Write-Host "Removed scheduled task '$TaskName'." -ForegroundColor Green
        $removed = $true
    } catch { }
    if (Test-Path $vbsPath) {
        Remove-Item $vbsPath -Force
        Write-Host "Removed Startup entry $vbsPath" -ForegroundColor Green
        $removed = $true
    }
    Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
        Where-Object { $_.CommandLine -like '*collector.py*' } |
        ForEach-Object {
            Stop-Process -Id $_.ProcessId -Force
            Write-Host "Stopped running collector (PID $($_.ProcessId))." -ForegroundColor Green
        }
    if (-not $removed) { Write-Host "Nothing was installed." -ForegroundColor Yellow }
    exit 0
}

# --------------------------------------------------------------- checks
if (-not (Test-Path $script))  { throw "collector.py not found next to this script." }
if (-not (Test-Path $envFile)) { throw "..\.env not found. Copy .env.example to .env first." }

$pythonw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $pythonw) {
    $py = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
    if (-not $py) { throw "Python is not on PATH. Install Python 3.10+ and retry." }
    $pythonw = Join-Path (Split-Path -Parent $py) 'pythonw.exe'
    if (-not (Test-Path $pythonw)) { throw "pythonw.exe not found beside $py" }
}

# --------------------------------------------------------------- install
$installed = $false

if (Test-Admin) {
    try {
        $action = New-ScheduledTaskAction -Execute $pythonw `
            -Argument "`"$script`"" -WorkingDirectory $here
        $trigger  = New-ScheduledTaskTrigger -AtLogOn
        $settings = New-ScheduledTaskSettingsSet `
            -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
            -RestartInterval (New-TimeSpan -Minutes 1) -RestartCount 3 `
            -ExecutionTimeLimit ([TimeSpan]::Zero)

        Register-ScheduledTask -TaskName $TaskName `
            -Action $action -Trigger $trigger -Settings $settings `
            -Description 'Records foreground window activity for the Activity Tracker dashboard.' `
            -Force | Out-Null

        Write-Host "Registered scheduled task '$TaskName' (starts at logon, restarts if it dies)." -ForegroundColor Green
        $installed = $true
    } catch {
        Write-Host "Scheduled task registration failed: $($_.Exception.Message)" -ForegroundColor Yellow
        Write-Host "Falling back to a Startup-folder entry." -ForegroundColor Yellow
    }
}

if (-not $installed) {
    # No elevation: a Startup shortcut works per-user and needs no rights.
    # WScript.Shell Run with window style 0 keeps it completely hidden.
    $vbs = @"
' Starts the Activity Tracker collector at logon, with no visible window.
' Remove this file to disable, or run install-startup.ps1 -Uninstall
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "$here"
sh.Run """$pythonw"" ""$script""", 0, False
"@
    Set-Content -Path $vbsPath -Value $vbs -Encoding ASCII
    Write-Host "Installed Startup entry (no admin needed):" -ForegroundColor Green
    Write-Host "  $vbsPath"
    if (-not (Test-Admin)) {
        Write-Host "Run this script from an elevated PowerShell to use a Scheduled Task instead," -ForegroundColor DarkGray
        Write-Host "which can also auto-restart the collector if it ever crashes." -ForegroundColor DarkGray
    }
}

# --------------------------------------------------------------- start now
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
    Where-Object { $_.CommandLine -like '*collector.py*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }

Start-Process -FilePath $pythonw -ArgumentList "`"$script`"" -WorkingDirectory $here -WindowStyle Hidden
Start-Sleep -Seconds 3

$running = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
           Where-Object { $_.CommandLine -like '*collector.py*' }
if ($running) {
    Write-Host "Collector is running now (PID $($running.ProcessId))." -ForegroundColor Green
} else {
    Write-Host "Collector did not stay running. Run collector\run-collector.bat to see the error." -ForegroundColor Red
}
