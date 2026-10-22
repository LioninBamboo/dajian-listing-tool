param(
    [string]$TaskName = 'GrovePop Finance Sync',
    [string]$ProjectRoot = 'C:\Users\poonx\GrovePop_Listing_Tool'
)

$ErrorActionPreference = 'Stop'
$template = Get-ScheduledTask -TaskName 'GrovePop Marketing Digest'
$python = $template.Actions[0].Execute
$action = New-ScheduledTaskAction `
    -Execute $python `
    -Argument "`"$ProjectRoot\scripts\finance_sync_orders.py`" --days 30" `
    -WorkingDirectory $ProjectRoot
$daily = New-ScheduledTaskTrigger -Daily -At '09:15'
$repeat = New-ScheduledTaskTrigger -Once -At (Get-Date).Date.AddHours(10) `
    -RepetitionInterval (New-TimeSpan -Hours 4) `
    -RepetitionDuration (New-TimeSpan -Hours 16)
$principal = $template.Principal
$settings = $template.Settings
Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger @($daily, $repeat) `
    -Principal $principal `
    -Settings $settings `
    -Force | Out-Null
Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State
Get-ScheduledTaskInfo -TaskName $TaskName | Select-Object LastRunTime, NextRunTime, LastTaskResult
