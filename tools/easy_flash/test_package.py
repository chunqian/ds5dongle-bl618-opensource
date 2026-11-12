"""Exercise launchers with a fake flasher; never opens or writes a real device.

python3 tools/easy_flash/test_package.py --package build/easy_flash/DS5Dongle-LCTech616-HS-HidAudio-R2-Flash-NativeMac --pwsh /path/to/pwsh
PowerShell on macOS validates script logic, not Windows drivers/native flashing.
"""
from pathlib import Path
import argparse
import hashlib
import os
import shutil
import subprocess
import tempfile


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def manifest(root):
    files = sorted(p for name in ('app', 'firmware', 'tools') for p in (root / name).rglob('*') if p.is_file())
    (root / 'asset-hashes.txt').write_text(''.join(f'{digest(p)}  {p.relative_to(root).as_posix()}\n' for p in files))


def fixture(base, package, platform, outcome, ports):
    root = base / '中文 空格目录' / platform
    root.mkdir(parents=True)
    shutil.copytree(package / 'app', root / 'app')
    for part in ('firmware', 'tools/common', 'tools/macos', 'tools/macos-arm64', 'tools/windows'):
        (root / part).mkdir(parents=True, exist_ok=True)
    shutil.copyfile(package / 'Flash-Mac.command', root / 'Flash-Mac.command')
    (root / 'firmware/version.txt').write_text('TEST FIXTURE - NO REAL DEVICE\n')
    for name in ('ds5dongle-lctech616-hs.bin', 'partition.bin', 'boot2_bl616_isp_release_v8.1.8.bin'):
        (root / 'firmware' / name).write_bytes(b'TEST FIXTURE ONLY\n')
    shutil.copyfile(package / 'firmware/flash_prog_cfg.ini', root / 'firmware/flash_prog_cfg.ini')
    (root / 'tools/common/test.conf').write_text('fixture\n')
    helper = root / 'app/list-ports-macos'
    helper.write_text('#!/bin/sh\n' + ''.join(f"printf '%s\\n' '{p}'\n" for p in ports if p.startswith('/dev/')))
    helper.chmod(0o755)
    expected = digest(root / 'firmware/ds5dongle-lctech616-hs.bin')
    lines = ['Verification succeeded'] * 3 + [f'SHA caled by host: {expected}', f'SHA caled by dev: {expected}',
             'Flash writing succeeded', 'All programming completed successfully']
    if outcome == 'no-verification': lines = ['Flash writing succeeded']
    if outcome == 'wrong-hash': lines = [line.replace(expected, '0' * 64) for line in lines]
    tool = root / 'tools' / platform / ('BLFlashCommand.exe' if platform == 'windows' else 'BLFlashCommand-macos')
    tool.write_text('#!/bin/sh\nif [ "${1:-}" = "--runtime-info" ]; then echo \'{"machine":"arm64","rosetta":false}\'; exit 0; fi\n'
                    'pwd > "$DS5_TEST_CALL_FILE"\nprintf "%s\\n" "$@" >> "$DS5_TEST_CALL_FILE"\n'
                    'test -f flash_prog_cfg.ini || exit 91\n'
                    'test -f ../tool/test.conf || exit 92\n'
                    'cat <<\'LOG\'\n' + '\n'.join(lines) + '\nLOG\n' +
                    ('exit 7\n' if outcome == 'exit-error' else 'exit 0\n'))
    tool.chmod(0o755)
    if platform == 'macos': shutil.copy2(tool, root / 'tools/macos-arm64/BLFlashCommand-macos')
    manifest(root)
    if outcome == 'corrupt': (root / 'firmware/ds5dongle-lctech616-hs.bin').write_bytes(b'CORRUPTED')
    return root


def run_case(package, pwsh, platform, outcome, count):
    with tempfile.TemporaryDirectory(prefix='ds5-launcher-test-') as directory:
        base = Path(directory)
        ports = ([f'COM{5+i}' for i in range(count)] if platform == 'windows' else
                 [f'/dev/cu.usbmodemTEST{i}' for i in range(count)])
        root = fixture(base, package, platform, outcome, ports)
        called = base / 'called.txt'
        env = dict(os.environ, DS5_TEST_CALL_FILE=str(called))
        if platform == 'macos':
            command = ['/bin/bash', str(root / 'Flash-Mac.command')]
        else:
            harness = base / 'run.ps1'
            records = ','.join("[pscustomobject]@{PNPDeviceID='USB\VID_349B&PID_6160\MOCK" + str(i) +
                               "';Name='Bouffalo (" + port + ")'}" for i, port in enumerate(ports))
            harness.write_text("$env:OS='Windows_NT'\nfunction Get-CimInstance { param($ClassName,$Filter) @(" + records + ") }\n"
                               "$global:DS5TestReads=0\nfunction Read-Host { param($Prompt) $global:DS5TestReads++; if($global:DS5TestReads -gt 1){ 'q' } else { '' } }\n"
                               "& $args[0]\nexit $LASTEXITCODE\n", encoding='utf-8-sig')
            command = [str(pwsh), '-NoLogo', '-NoProfile', '-File', str(harness), str(root / 'app/flash-windows.ps1')]
        result = subprocess.run(command, input='\nq\n', text=True, capture_output=True, env=env, timeout=30)
        output = result.stdout + result.stderr
        should_flash = count == 1 and outcome != 'corrupt'
        success = should_flash and outcome == 'success'
        assert called.exists() == should_flash, (platform, outcome, count, output)
        assert ('烧录成功，Boot2' in output) == success, (platform, outcome, count, output)
        if should_flash:
            lines = called.read_text().splitlines()
            assert not Path(lines[0]).exists(), ('temporary directory leaked', output)
            assert lines[1:] == ['--interface=uart', '--baudrate=2000000', '--port=' + ports[0],
                                '--chipname=bl616', '--config=flash_prog_cfg.ini'], lines
            assert list((root / 'logs').glob('*.log')), 'missing persistent log'
        if success or count != 1:
            assert result.returncode == 0, output
        else:
            assert result.returncode != 0, output
        print(f'PASS: {platform}: {outcome}, {count} download port(s)')


def test_powershell(package, pwsh):
    with tempfile.TemporaryDirectory(prefix='ds5-ps-test-') as temporary:
        path = Path(temporary) / 'test.ps1'
        path.write_text(r'''
$ErrorActionPreference='Stop'
foreach ($file in Get-ChildItem -LiteralPath (Join-Path $args[0] 'app') -Filter '*.ps1') {
    $tokens=$null; $errors=$null
    $null=[System.Management.Automation.Language.Parser]::ParseFile($file.FullName,[ref]$tokens,[ref]$errors)
    if($errors.Count) { throw ($errors | Out-String) }
}
. (Join-Path $args[0] 'app/flash-common.ps1')
$devices=@(
    [pscustomobject]@{PNPDeviceID='USB\VID_349B&PID_6160\A';Name='Boot ROM (COM5)'},
    [pscustomobject]@{PNPDeviceID='USB\VID_349B&PID_6160&MI_00\B';Name='Boot ROM (COM5)'},
    [pscustomobject]@{PNPDeviceID='USB\VID_054C&PID_0DF2\C';Name='DualSense (COM2)'},
    [pscustomobject]@{PNPDeviceID='USB\VID_349B&PID_61601\D';Name='Other (COM7)'},
    [pscustomobject]@{PNPDeviceID='USB\VID_349B&PID_6160\E';Name='No serial port'},
    [pscustomobject]@{PNPDeviceID='USB\VID_349B&PID_6160\F';Name='下载端口 (COM12)'}
)
$ports=@(Get-DownloadPortsFromDevices $devices)
if($ports.Count -ne 2 -or $ports -notcontains 'COM5' -or $ports -notcontains 'COM12') { throw "Wrong ports: $ports" }
Test-PackageAssets $args[0]
'PASS: PowerShell parsing, VID/PID filtering, duplicate ports, package integrity'
''', encoding='utf-8-sig')
        subprocess.run([str(pwsh), '-NoLogo', '-NoProfile', '-File', str(path), str(package)], check=True, timeout=30)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', type=Path, required=True)
    parser.add_argument('--pwsh', type=Path, required=True)
    args = parser.parse_args()
    package, pwsh = args.package.resolve(), args.pwsh.resolve()
    for hardware, process, expected in [('1','arm64','macos-arm64'), ('1','x86_64','macos-arm64'),
                                        ('0','x86_64','macos'), ('','arm64','macos-arm64')]:
        actual = subprocess.check_output(['/bin/bash','-c','source "$1"; mac_tool_subdir "$2" "$3"',
                                          'test',str(package/'app/flash-common.sh'),hardware,process],text=True).strip()
        assert actual == expected,(hardware,process,actual)
    print('PASS: Apple Silicon / Intel selection, including a translated parent shell')
    subprocess.run(['/bin/bash', str(package / 'Flash-Mac.command'), '--check'], check=True, timeout=30)
    test_powershell(package, pwsh)
    for platform in ('macos', 'windows'):
        for outcome, count in [('success',1), ('exit-error',1), ('no-verification',1), ('wrong-hash',1),
                               ('corrupt',1), ('success',0), ('success',2)]:
            run_case(package, pwsh, platform, outcome, count)
    print('PASS: no real device was opened or flashed by these tests')


if __name__ == '__main__':
    main()
