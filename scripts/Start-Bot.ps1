$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot "_Common.ps1")
$EnvPython = Resolve-BlueFishPython
if (-not (Test-Path -LiteralPath (Join-Path $Root ".env"))) {
    Write-Warning "未找到 .env，将使用当前进程环境变量。"
}

$env:PYTHONIOENCODING = "utf-8"
Push-Location $Root
try {
    & $EnvPython bot.py
    if ($LASTEXITCODE -ne 0) {
        throw "机器人已退出，退出码：$LASTEXITCODE"
    }
}
finally {
    Pop-Location
}

