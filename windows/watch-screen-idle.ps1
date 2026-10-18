# encoding: UTF-8
# version: 1.1 @20261018: 修复计划任务默认日志路径绑定失败。
# version: 1.0 @20261017: 夜间只读关屏诊断，采集输入空闲、电源和 Deskflow 时间线。
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][DateTimeOffset]$StartAt,
    [Parameter(Mandatory = $true)][DateTimeOffset]$EndAt,
    [string]$OutputDirectory = '',
    [ValidateRange(1, 60)][int]$IntervalSeconds = 5
)

$ErrorActionPreference = 'Stop'
if ($EndAt -le $StartAt) { throw 'EndAt must be later than StartAt.' }
if (-not $OutputDirectory) { $OutputDirectory = Join-Path $PSScriptRoot '..\data\screen-idle' }
$OutputDirectory = [IO.Path]::GetFullPath($OutputDirectory)
New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null
$runDirectory = Join-Path $OutputDirectory ($StartAt.ToString('yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Force -Path $runDirectory | Out-Null
$utf8 = New-Object System.Text.UTF8Encoding($false)
$writer = New-Object IO.StreamWriter((Join-Path $runDirectory 'timeline.jsonl'), $true, $utf8)
$writer.AutoFlush = $true
$isAdmin = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
$probe = $null
$deskflowReader = $null
$previous = $null
$nextDetails = [DateTimeOffset]::MinValue
$stats = [ordered]@{
    start_at = $StartAt.ToString('o'); end_at = $EndAt.ToString('o'); administrator = $isAdmin
    status = 'starting'; samples = 0; input_changes = 0; max_input_idle_seconds = 0
    display_required_samples = 0; system_required_samples = 0; power_query_failures = 0
    console_display_events = 0; session_display_events = 0; long_gaps = 0; error = $null
}

function Write-Record {
    param([string]$Kind, $Data)
    $writer.WriteLine((@{ time = [DateTimeOffset]::Now.ToString('o'); kind = $Kind; data = $Data } | ConvertTo-Json -Depth 8 -Compress))
}

function Write-Summary {
    $stats.updated_at = [DateTimeOffset]::Now.ToString('o')
    [IO.File]::WriteAllText((Join-Path $runDirectory 'summary.json'), ($stats | ConvertTo-Json -Depth 5), $utf8)
}

function Invoke-PowerCfg {
    param([string[]]$Arguments)
    # 明确禁止子进程弹出控制台，并限制查询耗时，避免诊断命令阻塞整夜。
    $info = New-Object Diagnostics.ProcessStartInfo
    $info.FileName = "$env:SystemRoot\System32\powercfg.exe"
    $info.Arguments = $Arguments -join ' '
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $process = New-Object Diagnostics.Process
    $process.StartInfo = $info
    try {
        $null = $process.Start()
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit(10000)) {
            $process.Kill()
            $process.WaitForExit()
            return @{ exit_code = -1; text = 'powercfg query timed out after 10 seconds'; timed_out = $true }
        }
        return @{ exit_code = $process.ExitCode; text = $stdout.Result + $stderr.Result; timed_out = $false }
    }
    finally { $process.Dispose() }
}

function Save-PowerSnapshot {
    param([string]$Label)
    $commands = @(
        @{ name = 'active-scheme'; args = @('/getactivescheme') },
        @{ name = 'display-settings'; args = @('/qh', 'SCHEME_CURRENT', 'SUB_VIDEO') },
        @{ name = 'sleep-settings'; args = @('/query', 'SCHEME_CURRENT', 'SUB_SLEEP') }
    )
    foreach ($command in $commands) {
        $result = Invoke-PowerCfg $command.args
        [IO.File]::WriteAllText((Join-Path $runDirectory "$Label-$($command.name).txt"), $result.text, $utf8)
        Write-Record 'powercfg_result' @{ label = $Label; command = $command.name; exit_code = $result.exit_code; timed_out = $result.timed_out }
    }
}

try {
    Write-Record 'start' @{ pid = $PID; computer = $env:COMPUTERNAME; administrator = $isAdmin; start_at = $StartAt.ToString('o'); end_at = $EndAt.ToString('o'); interval_seconds = $IntervalSeconds }
    while ([DateTimeOffset]::Now -lt $StartAt) { Start-Sleep -Seconds 1 }
    if ([DateTimeOffset]::Now -ge $EndAt) { throw 'The monitoring window has already ended.' }
    Add-Type -Path (Join-Path $PSScriptRoot 'ScreenIdleProbe.cs') -ReferencedAssemblies System.Windows.Forms
    $probe = New-Object ScreenIdleProbe
    Save-PowerSnapshot 'start'
    # 只记录进程名称、PID 和时间，不采集窗口标题、按键内容或屏幕截图。
    $stats.status = 'running'
    $stats.actual_start_at = [DateTimeOffset]::Now.ToString('o')
    $deskflowPath = Join-Path $env:ProgramData 'Deskflow\deskflow-daemon.log'
    if (Test-Path -LiteralPath $deskflowPath) {
        $stream = [IO.File]::Open($deskflowPath, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete)
        $null = $stream.Seek(0, [IO.SeekOrigin]::End)
        $deskflowReader = New-Object IO.StreamReader($stream)
    }
    Write-Record 'coverage' @{ power_requests_available = $isAdmin; note = 'Input timestamps do not identify the sender. Windows display state does not measure physical backlight.' }
    Write-Summary
    while ([DateTimeOffset]::Now -lt $EndAt) {
        if (Test-Path -LiteralPath (Join-Path $runDirectory 'STOP')) { $stats.status = 'stopped'; break }
        $now = [DateTimeOffset]::Now
        $reading = $probe.Sample()
        $powerChanged = $null -ne $previous -and $reading.ExecutionState -ne $previous.ExecutionState
        $stats.samples++
        if ($reading.InputAvailable) { $stats.max_input_idle_seconds = [Math]::Max($stats.max_input_idle_seconds, $reading.IdleSeconds) }
        if ($reading.PowerQueryStatus -ne 0) { $stats.power_query_failures++ }
        if ($reading.PowerQueryStatus -eq 0 -and ($reading.ExecutionState -band 2)) { $stats.display_required_samples++ }
        if ($reading.PowerQueryStatus -eq 0 -and ($reading.ExecutionState -band 1)) { $stats.system_required_samples++ }
        Write-Record 'sample' $reading
        if ($null -ne $previous) {
            if ($reading.InputAvailable -and $previous.InputAvailable -and $reading.LastInputTick -ne $previous.LastInputTick) {
                $stats.input_changes++
                Write-Record 'input_changed' @{ previous_idle_seconds = $previous.IdleSeconds; current_idle_seconds = $reading.IdleSeconds; foreground_process = $reading.ForegroundProcess; foreground_pid = $reading.ForegroundPid; attribution = 'unknown' }
            }
            if (($now - $previousTime).TotalSeconds -gt [Math]::Max(30, $IntervalSeconds * 3)) {
                $stats.long_gaps++
                Write-Record 'sampling_gap' @{ seconds = ($now - $previousTime).TotalSeconds; note = 'Possible suspend or collector delay; not proof of sleep.' }
            }
        }
        $previous = $reading
        $previousTime = $now
        foreach ($notice in $probe.Drain()) {
            Write-Record $notice.Kind $notice
            if ($notice.Kind -eq 'console_display') { $stats.console_display_events++ }
            if ($notice.Kind -eq 'session_display') { $stats.session_display_events++ }
        }
        if ($null -ne $deskflowReader) {
            # Deskflow 滚动/截断日志时重新定位；轮转后的证据缺口明确写入时间线。
            if ($deskflowReader.BaseStream.Length -lt $deskflowReader.BaseStream.Position) {
                $deskflowReader.DiscardBufferedData()
                $null = $deskflowReader.BaseStream.Seek(0, [IO.SeekOrigin]::Begin)
                Write-Record 'deskflow_log_truncated' @{}
            }
            while ($null -ne ($line = $deskflowReader.ReadLine())) {
                if ($line -match 'entering computer|leaving computer|started client|stopped client|connected to server|disconnected from server|wakeup|sleep|screen saver') {
                    Write-Record 'deskflow' @{ line = $line }
                }
            }
        }
        if ($now -ge $nextDetails -or $powerChanged) {
            if ($isAdmin) {
                Write-Record 'power_requests' (Invoke-PowerCfg @('/requests'))
            }
            $processes = @(Get-Process | Where-Object { $_.ProcessName -match 'deskflow|gameviewer|weixin|wechat|wallpaper|teamviewer|ChatGPT|chrome|msedge|powershell|pwsh|python' } | Select-Object Id, ProcessName, SessionId)
            Write-Record 'processes' $processes
            Write-Summary
            $nextDetails = $now.AddMinutes(1)
        }
        Start-Sleep -Seconds $IntervalSeconds
    }
    if ($stats.status -eq 'running') {
        if ($stats.samples -eq 0) { throw 'No samples collected before the monitoring deadline.' }
        $stats.status = 'complete'
    }
    Save-PowerSnapshot 'end'
}
catch {
    $stats.status = 'failed'
    $stats.error = $_.Exception.ToString()
    Write-Record 'error' @{ message = $stats.error }
}
finally {
    if ($null -ne $probe) {
        foreach ($notice in $probe.Drain()) { Write-Record $notice.Kind $notice }
        $probe.Dispose()
    }
    if ($null -ne $deskflowReader) { $deskflowReader.Dispose() }
    $stats.actual_end_at = [DateTimeOffset]::Now.ToString('o')
    Write-Record 'end' $stats
    Write-Summary
    $writer.Dispose()
}
if ($stats.status -eq 'failed') { Write-Error $stats.error; exit 1 }
Write-Output $runDirectory
