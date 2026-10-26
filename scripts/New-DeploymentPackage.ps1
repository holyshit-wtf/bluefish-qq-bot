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
if (-not (Test-Path -LiteralPath (Join-Path $Root ".env") -PathType Leaf)) {
    throw "完整部署包需要项目根目录的 .env。"
}
$EnvPython = Resolve-BlueFishPython
$env:PYTHONIOENCODING = "utf-8"

Push-Location $Root
try {
    & $EnvPython scripts\migration.py deployment `
        --project-root $Root `
        --output-directory $OutputDirectory
    if ($LASTEXITCODE -ne 0) {
        throw "完整部署包生成失败，退出码：$LASTEXITCODE"
    }
}
finally {
    Pop-Location
}
