param(
    [string]$ProjectRoot = "C:\Users\poonx\Dajian_Listing_Tool"
)

$ErrorActionPreference = 'Stop'

$logPath = Join-Path $ProjectRoot 'logs\deferred_scheduler_restart.log'
$pythonw = Join-Path $ProjectRoot '.venv\Scripts\pythonw.exe'
$daemonScript = Join-Path $ProjectRoot 'scheduler_daemon.py'
$managedTaskPatterns = @(
    '*daily_tasks.py*',
    '*daily_optimize.py*',
    '*sales_health_check.py*',
    '*auto_rotate_promotions.py*',
    '*batch_smart_reprice.py*'
)

function Write-Log {
    param([string]$Message)
    $timestamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    Add-Content -Path $logPath -Value "$timestamp $Message" -Encoding UTF8
}

function Get-ManagedTaskProcesses {
    Get-CimInstance Win32_Process | Where-Object {
        $proc = $_
        if ($proc.CommandLine -notlike '*Dajian_Listing_Tool*') {
            return $false
        }

        foreach ($pattern in $managedTaskPatterns) {
            if ($proc.CommandLine -like $pattern) {
                return $true
            }
        }

        return $false
    }
}

Write-Log '[DEFERRED] Waiting for managed task processes to exit'

while (Get-ManagedTaskProcesses) {
    Start-Sleep -Seconds 5
}

Write-Log '[DEFERRED] Managed tasks finished; restarting scheduler daemon'

Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -like '*scheduler_daemon.py*' -and $_.CommandLine -like '*Dajian_Listing_Tool*'
} | ForEach-Object {
    try {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop
        Write-Log ("[DEFERRED] Stopped old scheduler PID=" + $_.ProcessId)
    } catch {
        Write-Log ("[DEFERRED] Failed to stop scheduler PID=" + $_.ProcessId + ' : ' + $_.Exception.Message)
    }
}

Start-Sleep -Seconds 2

$newProc = Start-Process -FilePath $pythonw -ArgumentList $daemonScript -WorkingDirectory $ProjectRoot -WindowStyle Hidden -PassThru
Write-Log ("[DEFERRED] Started scheduler daemon PID=" + $newProc.Id)
