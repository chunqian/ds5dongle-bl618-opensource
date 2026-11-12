# SPDX-License-Identifier: GPL-3.0-or-later
function Get-DownloadPortsFromDevices {
    param([object[]]$Devices)
    $ports = foreach ($device in $Devices) {
        if ($device.PNPDeviceID -match '^USB\\VID_349B&PID_6160(?:&|\\)' -and
            $device.Name -match '\((COM[0-9]+)\)') {
            $Matches[1].ToUpperInvariant()
        }
    }
    @($ports | Sort-Object -Unique)
}

function Test-PackageAssets {
    param([string]$Root)
    $manifest = Join-Path $Root 'asset-hashes.txt'
    $lines = @(Get-Content -LiteralPath $manifest -ErrorAction Stop)
    if ($lines.Count -lt 6) { throw '安装包校验清单不完整。' }
    foreach ($line in $lines) {
        if ($line -notmatch '^([0-9a-f]{64})  ([a-zA-Z0-9_./-]+)$') {
            throw '安装包校验清单格式错误。'
        }
        $expected = $Matches[1]
        $relative = $Matches[2]
        if ($relative.StartsWith('/') -or $relative.Contains('..')) { throw '校验路径错误。' }
        $file = Join-Path $Root $relative
        if ((Get-FileHash -LiteralPath $file -Algorithm SHA256 -ErrorAction Stop).Hash -ne $expected) {
            throw "文件校验失败：${relative}。请重新解压完整安装包。"
        }
    }
}

function Test-FlashLog {
    param([string]$Text, [string]$ExpectedHash, [int]$ExitCode)
    return ($ExitCode -eq 0 -and
        $Text.Contains('All programming completed successfully') -and
        $Text.Contains('Flash writing succeeded') -and
        [regex]::Matches($Text, 'Verification succeeded').Count -ge 3 -and
        $Text.Contains("SHA caled by host: $ExpectedHash") -and
        $Text.Contains("SHA caled by dev: $ExpectedHash"))
}
