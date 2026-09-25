# Removes the "VastPriceTracker" scheduled task if present.
# Usage: powershell -ExecutionPolicy Bypass -File uninstall_task.ps1
$TaskName = 'VastPriceTracker'
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed task '$TaskName'."
} else {
    Write-Host "Task '$TaskName' not installed."
}
