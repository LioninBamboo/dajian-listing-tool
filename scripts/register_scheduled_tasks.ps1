[CmdletBinding()]
param(
    [string]$ProjectRoot = 'C:\Users\poonx\Dajian_Listing_Tool',
    [string]$TaskNamePrefix = 'Dajian',
    [ValidateSet('Interactive', 'S4U', 'Password')]
    [string]$LogonType = 'S4U',
    [ValidateSet('Highest', 'Limited')]
    [string]$RunLevel = 'Highest',
    [System.Management.Automation.PSCredential]$Credential
)

$ErrorActionPreference = 'Stop'

function Write-Step {
    param([string]$Message)
    Write-Host $Message -ForegroundColor Cyan
}

function Get-TaskSummary {
    param([string]$TaskName)

    $task = Get-ScheduledTask -TaskName $TaskName
    $info = Get-ScheduledTaskInfo -TaskName $TaskName

    [PSCustomObject]@{
        TaskName                 = $task.TaskName
        State                    = $task.State
        LastRunTime              = $info.LastRunTime
        NextRunTime              = $info.NextRunTime
        LastTaskResult           = $info.LastTaskResult
        WakeToRun                = $task.Settings.WakeToRun
        StartWhenAvailable       = $task.Settings.StartWhenAvailable
        AllowStartIfOnBatteries  = $task.Settings.AllowStartIfOnBatteries
        DontStopIfGoingOnBattery = $task.Settings.DontStopIfGoingOnBatteries
        MultipleInstances        = $task.Settings.MultipleInstances
        UserId                   = $task.Principal.UserId
        LogonType                = $task.Principal.LogonType
        RunLevel                 = $task.Principal.RunLevel
    }
}

function ConvertTo-PlainTextPassword {
    param([System.Management.Automation.PSCredential]$InputCredential)

    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($InputCredential.Password)
    try {
        [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
}

function Register-DajianTask {
    param(
        [string]$TaskName,
        [Microsoft.Management.Infrastructure.CimInstance]$Action,
        [Microsoft.Management.Infrastructure.CimInstance[]]$Trigger,
        [Microsoft.Management.Infrastructure.CimInstance]$Settings,
        [string]$UserId,
        [string]$LogonType,
        [string]$RunLevel,
        [System.Management.Automation.PSCredential]$Credential
    )

    if ($LogonType -eq 'Password') {
        if (-not $Credential) {
            throw "Password logon requires -Credential."
        }
        $password = ConvertTo-PlainTextPassword -InputCredential $Credential
        Register-ScheduledTask `
            -TaskName $TaskName `
            -Action $Action `
            -Trigger $Trigger `
            -Settings $Settings `
            -User $Credential.UserName `
            -Password $password `
            -RunLevel $RunLevel `
            -Force | Out-Null
        return
    }

    $principal = New-ScheduledTaskPrincipal `
        -UserId $UserId `
        -LogonType $LogonType `
        -RunLevel $RunLevel
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $Action `
        -Trigger $Trigger `
        -Settings $Settings `
        -Principal $principal `
        -Force | Out-Null
}

$ProjectRoot = [System.IO.Path]::GetFullPath($ProjectRoot)
$python = Join-Path $ProjectRoot '.venv\Scripts\pythonw.exe'
if (-not (Test-Path $python)) {
    $python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
}
if (-not (Test-Path $python)) {
    throw "Python not found under $ProjectRoot\.venv\Scripts"
}

$daemonScript = Join-Path $ProjectRoot 'scheduler_daemon.py'
$watchdogScript = Join-Path $ProjectRoot 'scheduler_watchdog.py'
if (-not (Test-Path $daemonScript)) {
    throw "Missing daemon script: $daemonScript"
}
if (-not (Test-Path $watchdogScript)) {
    throw "Missing watchdog script: $watchdogScript"
}

$userId = '{0}\{1}' -f $env:USERDOMAIN, $env:USERNAME
$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).
    IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin -and $RunLevel -eq 'Highest') {
    Write-Warning 'Current PowerShell is not elevated; using Limited run level for registration.'
    $RunLevel = 'Limited'
}
if ($LogonType -eq 'Password' -and -not $Credential) {
    $Credential = Get-Credential -UserName $userId -Message 'Enter the Windows password for unattended Dajian scheduler tasks.'
}

Write-Step '[1/4] Cleaning legacy tasks...'
foreach ($name in @(
    'eBay Daily Title Optimization',
    'eBay_Daily_Title_Optimization',
    'Dajian Daily Tasks',
    'Dajian Auto Analyze',
    'Dajian Inventory Sync'
)) {
    try {
        Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction Stop
    } catch {
        if ($_.Exception.Message -notmatch 'Cannot find the file specified|No MSFT_ScheduledTask') {
            throw
        }
    }
}
Write-Host 'OK'
Write-Host ''

$daemonTaskName = "$TaskNamePrefix Scheduler Daemon"
$watchdogTaskName = "$TaskNamePrefix Scheduler Watchdog"

Write-Host ("ProjectRoot={0} TaskPrefix={1}" -f $ProjectRoot, $TaskNamePrefix)
Write-Host ("Using LogonType={0}, RunLevel={1}, User={2}" -f $LogonType, $RunLevel, $userId)
Write-Host ''

Write-Step '[2/4] Registering daemon task...'
$daemonSettings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -DontStopOnIdleEnd `
    -StartWhenAvailable `
    -WakeToRun `
    -MultipleInstances IgnoreNew `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 5) `
    -ExecutionTimeLimit (New-TimeSpan -Hours 24)
$daemonAction = New-ScheduledTaskAction `
    -Execute $python `
    -Argument ('"{0}"' -f $daemonScript) `
    -WorkingDirectory $ProjectRoot
$daemonTriggers = @(
    (New-ScheduledTaskTrigger -AtLogOn -User $userId),
    (New-ScheduledTaskTrigger -Daily -At 8:50AM)
)
Register-DajianTask `
    -TaskName $daemonTaskName `
    -Action $daemonAction `
    -Trigger $daemonTriggers `
    -Settings $daemonSettings `
    -UserId $userId `
    -LogonType $LogonType `
    -RunLevel $RunLevel `
    -Credential $Credential
Write-Host 'OK'
Write-Host ''

Write-Step '[3/4] Registering watchdog task...'
$watchdogSettings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -WakeToRun `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 6)
$watchdogAction = New-ScheduledTaskAction `
    -Execute $python `
    -Argument ('"{0}"' -f $watchdogScript) `
    -WorkingDirectory $ProjectRoot
$watchdogTrigger = New-ScheduledTaskTrigger `
    -Once `
    -At ((Get-Date).Date.AddMinutes(1)) `
    -RepetitionInterval (New-TimeSpan -Minutes 5) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
Register-DajianTask `
    -TaskName $watchdogTaskName `
    -Action $watchdogAction `
    -Trigger $watchdogTrigger `
    -Settings $watchdogSettings `
    -UserId $userId `
    -LogonType $LogonType `
    -RunLevel $RunLevel `
    -Credential $Credential
Write-Host 'OK'
Write-Host ''

Write-Step '[4/4] Verifying tasks...'
Get-TaskSummary -TaskName $daemonTaskName | Format-List
Write-Host ''
Get-TaskSummary -TaskName $watchdogTaskName | Format-List
Write-Host ''

Write-Host 'Scheduled task registration completed.' -ForegroundColor Green
