$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$LockPath = Join-Path $Root "requirements.lock.txt"
. (Join-Path $PSScriptRoot "_Common.ps1")
$EnvPython = Resolve-BlueFishPython

$Version = & $EnvPython -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
if ($LASTEXITCODE -ne 0 -or $Version.Trim() -ne "3.12") {
    throw "bluefish-bot 环境不可用或不是 Python 3.12。"
}

$Packages = & $EnvPython -m pip freeze
if ($LASTEXITCODE -ne 0) {
    throw "pip freeze 失败，未写入锁定文件。"
}
$Header = "# Generated from bluefish-bot Python 3.12; do not edit manually."
Set-Content -LiteralPath $LockPath -Value @($Header) -Encoding utf8
Add-Content -LiteralPath $LockPath -Value $Packages -Encoding utf8
Write-Output "已写入 $LockPath"

