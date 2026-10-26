$ErrorActionPreference = "Stop"

function Get-NormalizedPath {
    param([Parameter(Mandatory = $true)][string]$Path)

    return [System.IO.Path]::GetFullPath($Path).TrimEnd('\', '/')
}

function Resolve-CondaExe {
    $Candidates = New-Object System.Collections.Generic.List[string]
    if ($env:CONDA_EXE) {
        $Candidates.Add($env:CONDA_EXE)
    }
    $CondaCommand = Get-Command conda.exe -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($CondaCommand) {
        $Candidates.Add($CondaCommand.Source)
    }

    $KnownRoots = @(
        (Join-Path $env:USERPROFILE "anaconda3"),
        (Join-Path $env:USERPROFILE "miniconda3"),
        (Join-Path $env:LOCALAPPDATA "anaconda3"),
        (Join-Path $env:LOCALAPPDATA "miniconda3"),
        "C:\ProgramData\Anaconda3",
        "C:\ProgramData\Miniconda3",
        "E:\Anaconda\Anconda3"
    )
    foreach ($Root in $KnownRoots) {
        if ($Root) {
            $Candidates.Add((Join-Path $Root "Scripts\conda.exe"))
        }
    }

    foreach ($Candidate in $Candidates) {
        if ($Candidate -and (Test-Path -LiteralPath $Candidate -PathType Leaf)) {
            return (Get-NormalizedPath $Candidate)
        }
    }
    throw "未找到 conda.exe。请先安装 Anaconda，或将 conda.exe 加入 PATH。"
}

function Get-BlueFishEnvironmentPrefix {
    param(
        [string]$CondaExe = ""
    )

    if (-not $CondaExe) {
        $CondaExe = Resolve-CondaExe
    }
    $ScriptsDirectory = Split-Path -Parent $CondaExe
    $CondaRoot = Split-Path -Parent $ScriptsDirectory
    $DirectPrefix = Join-Path $CondaRoot "envs\bluefish-bot"
    if (Test-Path -LiteralPath (Join-Path $DirectPrefix "python.exe")) {
        return (Get-NormalizedPath $DirectPrefix)
    }
    $EnvListJson = & $CondaExe env list --json 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw "无法读取 Conda 环境列表。"
    }
    try {
        $EnvList = $EnvListJson | ConvertFrom-Json
    }
    catch {
        throw "Conda 环境列表不是有效 JSON。"
    }
    foreach ($Prefix in $EnvList.envs) {
        if ([System.IO.Path]::GetFileName($Prefix) -eq "bluefish-bot") {
            return (Get-NormalizedPath $Prefix)
        }
    }
    return $null
}

function Resolve-BlueFishPython {
    $CondaExe = Resolve-CondaExe
    $Prefix = Get-BlueFishEnvironmentPrefix -CondaExe $CondaExe
    if (-not $Prefix) {
        throw "未找到 bluefish-bot Conda 环境，请先运行 scripts\Create-Environment.ps1。"
    }
    $Python = Join-Path $Prefix "python.exe"
    if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
        throw "bluefish-bot 环境缺少 python.exe：$Python"
    }
    return (Get-NormalizedPath $Python)
}

function Get-BlueFishProcessInfo {
    param(
        [Parameter(Mandatory = $true)][string]$LockPath
    )

    if (-not (Test-Path -LiteralPath $LockPath -PathType Leaf)) {
        return $null
    }
    $PidText = (Get-Content -Raw -LiteralPath $LockPath).Trim()
    if ($PidText -notmatch '^\d+$') {
        return $null
    }
    $Process = Get-CimInstance Win32_Process -Filter "ProcessId=$PidText" `
        -ErrorAction SilentlyContinue
    if (-not $Process) {
        return $null
    }
    $CommandLine = [string]$Process.CommandLine
    if ($CommandLine -notmatch '(?i)(^|[\\/ "\s])bot\.py(["\\/\s]|$)') {
        return $null
    }
    return [PSCustomObject]@{
        ProcessId = [int]$PidText
        ExecutablePath = [string]$Process.ExecutablePath
        CommandLine = $CommandLine
    }
}

function Assert-BlueFishStopped {
    param(
        [Parameter(Mandatory = $true)][string]$LockPath
    )

    $Process = Get-BlueFishProcessInfo -LockPath $LockPath
    if ($Process) {
        throw (
            "机器人仍在运行（PID $($Process.ProcessId)）。" +
            "请先运行 scripts\Stop-Bot.ps1 并关闭 QQ/NapCat。"
        )
    }
}

function Clear-BlueFishStaleLock {
    param(
        [Parameter(Mandatory = $true)][string]$LockPath
    )

    $Process = Get-BlueFishProcessInfo -LockPath $LockPath
    if (-not $Process -and (Test-Path -LiteralPath $LockPath)) {
        Remove-Item -LiteralPath $LockPath -Force
    }
}
