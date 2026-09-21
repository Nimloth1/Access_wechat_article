# encoding: UTF-8
param(
    [Parameter(Mandatory = $true)]
    [ValidateRange(1, 65535)]
    [int]$SessionId,

    [string]$ExpectedUser = 'DESKTOP-V8117LP\maegl'
)

$ErrorActionPreference = 'Stop'
$logDirectory = Join-Path $env:ProgramData 'CodexRdpConsole'
$logPath = Join-Path $logDirectory 'return-to-console.log'
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null

function Write-TaskLog {
    param([string]$Message)

    $timestamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss.fff'
    Add-Content -LiteralPath $logPath -Value "[$timestamp] $Message" -Encoding UTF8
}

try {
    Write-TaskLog "RDP disconnect event received: user=$ExpectedUser session=$SessionId"
    $output = & "$env:SystemRoot\System32\tscon.exe" $SessionId /dest:console 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "tscon exit code $LASTEXITCODE`: $($output -join ' ')"
    }
    Write-TaskLog "Session $SessionId returned to console: $($output -join ' ')"
}
catch {
    Write-TaskLog "Transfer failed: $($_.Exception.Message)"
    exit 1
}
