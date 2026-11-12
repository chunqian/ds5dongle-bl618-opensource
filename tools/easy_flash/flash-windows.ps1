# SPDX-License-Identifier: GPL-3.0-or-later
param([switch]$CheckOnly)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
$Root = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'flash-common.ps1')
$stage = $null
$result = 1
try {
    if ($env:OS -ne 'Windows_NT' -or -not [Environment]::Is64BitOperatingSystem) {
        throw '此版本需要 64 位 Windows 10 / 11。'
    }
    Write-Host "`nDS5Dongle 简易烧录工具"
    Get-Content -LiteralPath (Join-Path $Root 'firmware/version.txt') -Encoding UTF8 | Write-Host
    Write-Host "仅用于 LCTech BL616 适配器。`n"
    Test-PackageAssets $Root
    Write-Host '安装包文件校验通过。'
    if ($CheckOnly) {
        Get-DownloadPortsFromDevices @(Get-CimInstance Win32_PnPEntity -Filter "PNPClass='Ports'") | Write-Host
        Write-Host '环境检查通过；未执行烧录。'
        exit 0
    }
    Write-Host "`n请按住适配器 BOOT 键插入 USB，再松开 BOOT。"
    Write-Host '退出占用串口的程序，每次只接一个待刷适配器。'
    while ($true) {
        $answer = Read-Host '准备好后按回车开始，输入 q 退出'
        if ($answer -eq 'q') { exit 0 }
        $ports = @(Get-DownloadPortsFromDevices @(Get-CimInstance Win32_PnPEntity -Filter "PNPClass='Ports'"))
        if ($ports.Count -eq 0) {
            Write-Host '未检测到 BOOT 下载端口。请按住 BOOT 重新插入，并检查 USB 数据线。'
        } elseif ($ports.Count -gt 1) {
            Write-Host '检测到多个下载端口，请仅保留一个待刷适配器。'
        } else { break }
    }
    $portName = $ports[0]
    $logDir = Join-Path $Root 'logs'
    $null = New-Item -ItemType Directory -Force -Path $logDir
    $log = Join-Path $logDir ('windows-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + $PID + '.log')
    $stage = Join-Path ([IO.Path]::GetTempPath()) ('DS5Flash-' + [Guid]::NewGuid().ToString('N'))
    $null = New-Item -ItemType Directory -Path $stage
    $toolDir = Join-Path $stage 'tool'
    Copy-Item -LiteralPath (Join-Path $Root 'tools/windows') -Destination $toolDir -Recurse
    Get-ChildItem -LiteralPath (Join-Path $Root 'tools/common') | Copy-Item -Destination $toolDir -Recurse
    Copy-Item -LiteralPath (Join-Path $Root 'firmware') -Destination $stage -Recurse
    $firmwareDir = Join-Path $stage 'firmware'
    $expected = (Get-FileHash -LiteralPath (Join-Path $firmwareDir 'ds5dongle-lctech616-hs.bin') -Algorithm SHA256).Hash.ToLowerInvariant()
    Write-Host "`n已找到下载端口：$portName"
    Write-Host '正在烧录并校验，通常需要 10–30 秒，请勿拔线。'
    Write-Host "日志：$log`n"
    Push-Location -LiteralPath $firmwareDir
    try {
        # Windows PowerShell 5.1 wraps stderr in ErrorRecord. Do not abort before
        # collecting the vendor program's exit code and complete verification log.
        $ErrorActionPreference = 'Continue'
        & (Join-Path $toolDir 'BLFlashCommand.exe') '--interface=uart' '--baudrate=2000000' "--port=$portName" '--chipname=bl616' '--config=flash_prog_cfg.ini' 2>&1 |
            ForEach-Object { $_.ToString() } | Tee-Object -FilePath $log | ForEach-Object { Write-Host $_ }
        $flashExit = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = 'Stop'
        Pop-Location
    }
    $text = Get-Content -LiteralPath $log -Raw
    if (-not (Test-FlashLog $text $expected $flashExit)) {
        throw "烧录或回读校验未通过。请保留日志：${log}；按住 BOOT 重新插入后重试。"
    }
    Write-Host "`n烧录成功，Boot2、分区表和固件均已校验。" -ForegroundColor Green
    Write-Host '请不按 BOOT，拔插一次适配器即可使用。'
    $result = 0
} catch {
    Write-Host "`n失败：$($_.Exception.Message)" -ForegroundColor Red
} finally {
    if ($stage -and (Test-Path -LiteralPath $stage)) {
        Remove-Item -LiteralPath $stage -Recurse -Force -ErrorAction SilentlyContinue
    }
}
exit $result
