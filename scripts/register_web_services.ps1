[CmdletBinding()]
param(
    [string]$ProjectRoot = '',
    [string]$TaskNamePrefix = '',
    [string]$UserId = '',
    [ValidateSet('Interactive', 'S4U', 'Password')]
    [string]$LogonType = 'Interactive',
    [ValidateSet('Highest', 'Limited')]
    [string]$RunLevel = 'Limited',
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
        TaskName                = $task.TaskName
        State                   = $task.State
        LastRunTime             = $info.LastRunTime
        NextRunTime             = $info.NextRunTime
        LastTaskResult          = $info.LastTaskResult
        MultipleInstances       = $task.Settings.MultipleInstances
        UserId                  = $task.Principal.UserId
        LogonType               = $task.Principal.LogonType
        RunLevel                = $task.Principal.RunLevel
        Actions                 = ($task.Actions | ForEach-Object { "$($_.Execute) $($_.Arguments)" }) -join '; '
    }
}

if (-not $ProjectRoot) {
    $ProjectRoot = Split-Path -Parent $PSScriptRoot
}
$ProjectRoot = [System.IO.Path]::GetFullPath($ProjectRoot)

$startWeb = Join-Path $ProjectRoot 'start_web.bat'
if (-not (Test-Path $startWeb)) {
    throw "Missing start_web.bat at $startWeb"
}

$python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
    throw "Python not found under $ProjectRoot\.venv\Scripts"
}

# Resolve brand/ports from THIS checkout's .env + store profile. Abort on failure —
# never fall back to a generic 'Dajian Web Services' task name.
# Parse stdout only (no 2>&1): PS 5.1 merges stderr warnings into the success
# stream and would break ConvertFrom-Json.
$portJson = & $python -m src.utils.store_ports --format json | Out-String
if ($LASTEXITCODE -ne 0) {
    throw "store_ports lookup failed (exit=$LASTEXITCODE)"
}
try {
    $ports = $portJson | ConvertFrom-Json
} catch {
    throw "store_ports returned non-JSON stdout: $portJson"
}
$brand = [string]$ports.brand_name
if (-not $brand) {
    throw 'store_ports returned empty brand_name'
}
if (-not $ports.server_port -or -not $ports.streamlit_port) {
    throw 'store_ports returned empty server_port/streamlit_port'
}
if (-not $TaskNamePrefix) {
    $safe = ($brand -replace '[^A-Za-z0-9_-]', '')
    if (-not $safe) {
        throw "Unable to derive TaskNamePrefix from brand '$brand'"
    }
    $TaskNamePrefix = $safe
}

$taskName = "$TaskNamePrefix Web Services"
if (-not $UserId) {
    $UserId = '{0}\{1}' -f $env:USERDOMAIN, $env:USERNAME
}
if (-not $UserId) {
    throw 'UserId is required (pass -UserId explicitly or ensure USERDOMAIN/USERNAME are set)'
}

Write-Host ("ProjectRoot={0}" -f $ProjectRoot)
Write-Host ("Brand={0} FastAPI={1} Streamlit={2}" -f $brand, $ports.server_port, $ports.streamlit_port)
Write-Host ("TaskName={0} LogonType={1} RunLevel={2} UserId={3}" -f $taskName, $LogonType, $RunLevel, $UserId)
Write-Host ''
Write-Host 'This registers WEB-ONLY auto-start (FastAPI+Streamlit).' -ForegroundColor Yellow
Write-Host 'It does NOT start scheduler_daemon. Use setup_scheduled_tasks.bat for that.' -ForegroundColor Yellow
Write-Host ''

Write-Step '[1/2] Registering logon web-services task (idempotent -Force)...'
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1)
# cmd /c start_web.bat — bat itself skips ports already LISTENING (no duplicates)
$action = New-ScheduledTaskAction `
    -Execute 'cmd.exe' `
    -Argument ('/c ""{0}""' -f $startWeb) `
    -WorkingDirectory $ProjectRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $UserId

if ($LogonType -eq 'Password') {
    if (-not $Credential) {
        $Credential = Get-Credential -UserName $UserId -Message 'Password for web-services logon task'
    }
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Credential.Password)
    try {
        $password = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
    Register-ScheduledTask `
        -TaskName $taskName `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -User $Credential.UserName `
        -Password $password `
        -RunLevel $RunLevel `
        -Force | Out-Null
} else {
    $principal = New-ScheduledTaskPrincipal `
        -UserId $UserId `
        -LogonType $LogonType `
        -RunLevel $RunLevel
    # -UserId is set explicitly on the principal (required; no implicit current-user fallback).
    Register-ScheduledTask `
        -TaskName $taskName `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Principal $principal `
        -Force | Out-Null
}
Write-Host 'OK'
Write-Host ''

Write-Step '[2/2] Verifying...'
Get-TaskSummary -TaskName $taskName | Format-List
Write-Host ''
Write-Host 'Web services logon task registered (or updated).' -ForegroundColor Green
Write-Host 'Re-run this script any time; -Force + MultipleInstances IgnoreNew keep it idempotent.'
Write-Host ("Manual smoke: {0}" -f $startWeb)
