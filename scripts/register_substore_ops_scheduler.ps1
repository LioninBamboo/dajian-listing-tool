[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('GrovePop', 'AquaRides')]
    [string]$StorePrefix,

    [string]$ProjectRoot = '',
    [ValidateSet('Interactive', 'S4U', 'Password')]
    [string]$LogonType = 'S4U',
    [ValidateSet('Highest', 'Limited')]
    [string]$RunLevel = 'Highest',
    [System.Management.Automation.PSCredential]$Credential
)

$ErrorActionPreference = 'Stop'

$defaults = @{
    GrovePop   = 'C:\Users\poonx\GrovePop_Listing_Tool'
    AquaRides  = 'C:\Users\poonx\AutoParts_Listing_Tool'
}

if (-not $ProjectRoot) {
    $ProjectRoot = $defaults[$StorePrefix]
}

$registerScript = Join-Path $PSScriptRoot 'register_scheduled_tasks.ps1'
if (-not (Test-Path $registerScript)) {
    throw "Missing register_scheduled_tasks.ps1 at $registerScript"
}

Write-Host "Registering sub-store ops scheduler for $StorePrefix at $ProjectRoot" -ForegroundColor Cyan
Write-Host "Prerequisite: config/store_profile.local.yaml must set scheduler_profile: ops" -ForegroundColor Yellow
Write-Host ""

& $registerScript `
    -ProjectRoot $ProjectRoot `
    -TaskNamePrefix $StorePrefix `
    -LogonType $LogonType `
    -RunLevel $RunLevel `
    -Credential $Credential
