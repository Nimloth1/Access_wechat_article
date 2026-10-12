# AWA interactive desktop launcher; token stays in the PW user environment.
$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'
Set-Location -LiteralPath $PSScriptRoot
$env:AWA_AGENT_TOKEN = [Environment]::GetEnvironmentVariable('AWA_AGENT_TOKEN', 'User')
$env:PYTHONIOENCODING = 'utf-8'
if (-not $env:AWA_AGENT_TOKEN) { throw 'AWA_AGENT_TOKEN is not configured for this user' }
$taskLogDirectory = Join-Path $PSScriptRoot 'data\logs'
New-Item -ItemType Directory -Path $taskLogDirectory -Force | Out-Null
& "$PSScriptRoot\.venv\Scripts\python.exe" "$PSScriptRoot\awa_agent.py" serve `
    1>> "$taskLogDirectory\awa_agent_serve.out.log" `
    2>> "$taskLogDirectory\awa_agent_serve.err.log"
exit $LASTEXITCODE
