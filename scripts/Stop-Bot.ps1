$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$LockPath = Join-Path $Root "data\bot.lock"
. (Join-Path $PSScriptRoot "_Common.ps1")
$ExpectedPython = Resolve-BlueFishPython

if (-not (Test-Path -LiteralPath $LockPath)) {
    Write-Output "未发现运行锁，机器人可能未启动。"
    exit 0
}
$Process = Get-BlueFishProcessInfo -LockPath $LockPath
if (-not $Process) {
    Write-Output "运行锁内容无效或锁内进程已不存在，未执行停止操作。"
    exit 1
}
if (
    (Get-NormalizedPath $Process.ExecutablePath) -ne
    (Get-NormalizedPath $ExpectedPython)
) {
    Write-Output "锁内进程不是本项目 bot.py，未执行停止操作。"
    exit 1
}
$PidText = [string]$Process.ProcessId
Stop-Process -Id $Process.ProcessId
Wait-Process -Id $Process.ProcessId -ErrorAction SilentlyContinue
if (
    (Test-Path -LiteralPath $LockPath) -and
    ((Get-Content -Raw -LiteralPath $LockPath).Trim() -eq $PidText)
) {
    Remove-Item -LiteralPath $LockPath -Force
}
Write-Output "已停止机器人进程 $PidText。"

