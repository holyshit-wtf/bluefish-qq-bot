$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot "_Common.ps1")
$EnvPython = Resolve-BlueFishPython
$env:PYTHONIOENCODING = "utf-8"

Push-Location $Root
try {
    & $EnvPython scripts/healthcheck.py
    if ($LASTEXITCODE -ne 0) {
        throw "健康检查失败。"
    }
}
finally {
    Pop-Location
}

