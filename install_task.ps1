# Registers (or replaces) the "VastPriceTracker" scheduled task: run.cmd hourly at :17,
# current user, on battery too, catches up after sleep, no console window.
# Usage: powershell -ExecutionPolicy Bypass -File install_task.ps1
$ErrorActionPreference = 'Stop'
$TaskName = 'VastPriceTracker'
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$RunCmd = Join-Path $Here 'run.cmd'
if (-not (Test-Path $RunCmd)) { throw "run.cmd not found next to this script: $RunCmd" }

# conhost --headless runs the console app with no visible window (Windows 10 1809+/11).
$Action = New-ScheduledTaskAction -Execute "$env:WINDIR\System32\conhost.exe" `
    -Argument "--headless `"$env:WINDIR\System32\cmd.exe`" /c `"`"$RunCmd`"`"" `
    -WorkingDirectory $Here

# First run: the next :17 from now, then every hour indefinitely.
$now = Get-Date
$start = $now.Date.AddHours($now.Hour).AddMinutes(17)
if ($start -le $now) { $start = $start.AddHours(1) }
$Trigger = New-ScheduledTaskTrigger -Once -At $start -RepetitionInterval (New-TimeSpan -Hours 1)

$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 15)

$User = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$Principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed existing task '$TaskName'."
}
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings `
    -Principal $Principal -Description 'Hourly Vast.ai RTX 3090/4090/5090 price snapshot (vast-price-tracker/run.cmd)' | Out-Null

$info = Get-ScheduledTaskInfo -TaskName $TaskName
Write-Host "Registered '$TaskName' for $User. Next run: $($info.NextRunTime)"
