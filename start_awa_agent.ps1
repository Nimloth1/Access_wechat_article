# AWA interactive desktop launcher; token stays in the PW user environment.
$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'
Set-Location -LiteralPath $PSScriptRoot
$env:AWA_AGENT_TOKEN = [Environment]::GetEnvironmentVariable('AWA_AGENT_TOKEN', 'User')
$env:PYTHONIOENCODING = 'utf-8'
if (-not $env:AWA_AGENT_TOKEN) { throw 'AWA_AGENT_TOKEN is not configured for this user' }
$taskLogDirectory = Join-Path $PSScriptRoot 'data\logs'
New-Item -ItemType Directory -Path $taskLogDirectory -Force | Out-Null
$taskLaunchStamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$taskProcess = Start-Process -FilePath "$PSScriptRoot\.venv\Scripts\python.exe" `
    -ArgumentList ('"{0}" serve --window-messages' -f "$PSScriptRoot\awa_agent.py") `
    -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -Wait -PassThru `
    -RedirectStandardOutput "$taskLogDirectory\awa_agent_serve-$taskLaunchStamp.out.log" `
    -RedirectStandardError "$taskLogDirectory\awa_agent_serve-$taskLaunchStamp.err.log"
exit $taskProcess.ExitCode
