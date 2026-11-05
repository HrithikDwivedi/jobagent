<#
Registers (or removes) a Windows Task Scheduler job that runs the JobAgent agent daily.
The scheduled run discovers, scores and prepares drafts only; it can never submit an application.
Overlapping runs are prevented by the database lock.

  powershell -ExecutionPolicy Bypass -File scripts\schedule_windows.ps1 -Time 08:30
  powershell -ExecutionPolicy Bypass -File scripts\schedule_windows.ps1 -Remove
#>
param(
    [string]$Time = "08:30",
    [string]$Days = "MON,TUE,WED,THU,FRI",
    [switch]$Remove
)
$ErrorActionPreference = "Stop"
$TaskName = "JobAgent Daily Run"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$Log = Join-Path $Root "data\scheduled-run.log"

if ($Remove) {
    schtasks /Delete /TN $TaskName /F
    Write-Output "Removed scheduled task '$TaskName'."
    exit 0
}
if (-not (Test-Path $Python)) { throw "Virtualenv not found at $Python. Follow the README installation steps first." }
New-Item -ItemType Directory -Force (Join-Path $Root "data") | Out-Null
$Command = "cmd /c cd /d `"$Root`" && `"$Python`" -m jobagent run >> `"$Log`" 2>&1"
schtasks /Create /TN $TaskName /TR $Command /SC WEEKLY /D $Days /ST $Time /F | Out-Null
Write-Output "Scheduled '$TaskName' at $Time on $Days. Log: $Log"
