[CmdletBinding()]
param(
    [string]$ProjectRoot = 'C:\Users\poonx\GrovePop_Listing_Tool',
    [datetime]$RunAt = [datetime]::Today.AddHours(11).AddMinutes(30),
    # Default Disabled: GrovePop Scheduler Daemon owns 11:30 detect + 12:10 whitelist fix.
    # Re-enable only for emergency detect-only runs (never pass bare --fix).
    [switch]$Enabled
)

$ErrorActionPreference = 'Stop'

$ProjectRoot = [System.IO.Path]::GetFullPath($ProjectRoot)
$pythonw = Join-Path $ProjectRoot '.venv\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $pythonw)) {
    throw "pythonw.exe not found: $pythonw"
}

$qcScript = Join-Path $ProjectRoot 'scripts\audit_fix_active_listings.py'
if (-not (Test-Path -LiteralPath $qcScript)) {
    throw "QC script not found: $qcScript"
}

$taskName = 'GrovePop Content QC'
$userId = '{0}\{1}' -f $env:USERDOMAIN, $env:USERNAME
# RED LINE: never register bare --fix without --fix-key (Outdoor Daybeds class of incident).
$action = New-ScheduledTaskAction `
    -Execute $pythonw `
    -Argument ('"{0}" --live --email --record-clean-state --exit-zero-on-issues' -f $qcScript) `
    -WorkingDirectory $ProjectRoot
$trigger = New-ScheduledTaskTrigger -Daily -At $RunAt
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 6)
if (-not $Enabled) {
    $settings.Enabled = $false
}
$principal = New-ScheduledTaskPrincipal `
    -UserId $userId `
    -LogonType Interactive `
    -RunLevel Limited

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Force | Out-Null

$task = Get-ScheduledTask -TaskName $taskName
$info = Get-ScheduledTaskInfo -TaskName $taskName
[PSCustomObject]@{
    TaskName = $task.TaskName
    State = $task.State
    Enabled = $task.Settings.Enabled
    Execute = $task.Actions.Execute
    Arguments = $task.Actions.Arguments
    WorkingDirectory = $task.Actions.WorkingDirectory
    NextRunTime = $info.NextRunTime
}
