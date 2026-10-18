# encoding: UTF-8
# version: 1.1 @20261018: 显式指定监控日期，按开始日期命名任务。
# version: 1.0 @20261017: 安装一次性的交互桌面关屏诊断任务。
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][DateTimeOffset]$StartAt,
    [Parameter(Mandatory = $true)][DateTimeOffset]$EndAt,
    [string]$TaskName = ''
)

$ErrorActionPreference = 'Stop'
if ($EndAt -le $StartAt -or $EndAt -le [DateTimeOffset]::Now) { throw 'Invalid or expired monitoring window.' }
if (-not $TaskName) { $TaskName = 'Codex-Screen-Idle-' + $StartAt.ToString('yyyyMMdd') }
$scriptPath = Join-Path $PSScriptRoot 'watch-screen-idle.ps1'
if (-not (Test-Path -LiteralPath $scriptPath)) { throw "Collector missing: $scriptPath" }
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$isAdmin = [Security.Principal.WindowsPrincipal]::new($identity).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
$level = if ($isAdmin) { 'Highest' } else { 'Limited' }
$principal = New-ScheduledTaskPrincipal -UserId $identity.Name -LogonType Interactive -RunLevel $level
$arguments = '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}" -StartAt "{1}" -EndAt "{2}"' -f $scriptPath, $StartAt.ToString('o'), $EndAt.ToString('o')
$action = New-ScheduledTaskAction -Execute "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" -Argument $arguments -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -Once -At $StartAt.LocalDateTime
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 12) -MultipleInstances IgnoreNew
$task = Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Description 'One-night read-only display/input/power-request diagnostics. Does not inhibit sleep or send input.' -Force
[pscustomobject]@{
    task_name = $task.TaskName; state = [string]$task.State; run_level = $level
    start_at = $StartAt.ToString('o'); end_at = $EndAt.ToString('o'); power_requests_available = $isAdmin
} | ConvertTo-Json
