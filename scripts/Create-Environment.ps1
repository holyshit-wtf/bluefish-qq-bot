$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot "_Common.ps1")
$CondaExe = Resolve-CondaExe
$ExistingPrefix = Get-BlueFishEnvironmentPrefix -CondaExe $CondaExe

if ($ExistingPrefix) {
    throw "bluefish-bot 环境已存在：$ExistingPrefix。为避免意外覆盖，本脚本不会自动更新已有环境。"
}

Push-Location $Root
try {
    $env:PIP_INDEX_URL = "https://pypi.tuna.tsinghua.edu.cn/simple"
    & $CondaExe env create -f environment.lock.yml
    if ($LASTEXITCODE -ne 0) {
        throw "Conda 环境创建失败，退出码：$LASTEXITCODE"
    }
}
finally {
    Pop-Location
}

