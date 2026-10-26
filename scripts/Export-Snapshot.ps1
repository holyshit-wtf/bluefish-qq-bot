[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$OutputDirectory
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot "_Common.ps1")

$LockPath = Join-Path $Root "data\bot.lock"
Assert-BlueFishStopped -LockPath $LockPath
$EnvPython = Resolve-BlueFishPython
$env:PYTHONIOENCODING = "utf-8"

Push-Location $Root
try {
    & $EnvPython scripts\migration.py snapshot `
        --project-root $Root `
        --output-directory $OutputDirectory
    if ($LASTEXITCODE -ne 0) {
        throw "数据快照导出失败，退出码：$LASTEXITCODE"
    }
}
finally {
    Pop-Location
}
