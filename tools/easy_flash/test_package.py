"""Exercise launchers with a fake flasher; never opens or writes a real device.

python3 tools/easy_flash/test_package.py --package build/easy_flash/DS5Dongle-LCTech616-HS-HidAudio-R6-Flash-Windows --pwsh /path/to/pwsh
PowerShell on macOS validates script logic, not Windows drivers/native flashing.
"""
from pathlib import Path
import argparse
import hashlib
import json
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
    parts = ['firmware', 'tools/common', 'tools/' + platform]
    if platform == 'macos': parts.append('tools/macos-arm64')
    for part in parts:
        (root / part).mkdir(parents=True, exist_ok=True)
    launcher = 'Flash-Mac.command' if platform == 'macos' else 'Flash-Windows.bat'
    shutil.copyfile(package / launcher, root / launcher)
    (root / 'firmware/version.txt').write_text('TEST FIXTURE - NO REAL DEVICE\n')
    for name in ('ds5dongle-lctech616-hs.bin', 'partition.bin', 'boot2_bl616_isp_release_v8.1.8.bin'):
        (root / 'firmware' / name).write_bytes(b'TEST FIXTURE ONLY\n')
    shutil.copyfile(package / 'firmware/flash_prog_cfg.ini', root / 'firmware/flash_prog_cfg.ini')
    (root / 'tools/common/test.conf').write_text('fixture\n')
    if platform == 'macos':
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


def run_case(package, pwsh, platform, outcome, count, external_logs=False):
    with tempfile.TemporaryDirectory(prefix='ds5-launcher-test-') as directory:
        base = Path(directory)
        ports = ([f'COM{5+i}' for i in range(count)] if platform == 'windows' else
                 [f'/dev/cu.usbmodemTEST{i}' for i in range(count)])
        root = fixture(base, package, platform, outcome, ports)
        called = base / 'called.txt'
        env = dict(os.environ, DS5_TEST_CALL_FILE=str(called))
        env.pop('DS5_FLASH_LOG_DIR', None)
        log_dir = base / '用户 日志' if external_logs else root / 'logs'
        if external_logs:
            env['DS5_FLASH_LOG_DIR'] = str(log_dir)
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
            assert list(log_dir.glob('*.log')), 'missing persistent log'
            if external_logs:
                assert not (root / 'logs').exists(), 'log written inside signed payload'
        if success or count != 1:
            assert result.returncode == 0, output
        else:
            assert result.returncode != 0, output
        print(f'PASS: {platform}: {outcome}, {count} download port(s), external logs={external_logs}')


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


def test_layout(package):
    metadata = json.loads((package / 'package-info.json').read_text())
    platform = metadata['platform']
    assert platform in ('windows', 'macos'), platform
    mac = platform == 'macos'
    assert {p.name for p in (package / 'tools').iterdir()} == (
        {'common', 'macos', 'macos-arm64'} if mac else {'common', 'windows'})
    assert {p.name for p in (package / 'app').iterdir()} == (
        {'flash-mac.sh', 'flash-common.sh', 'list-ports-macos'} if mac else
        {'flash-windows.ps1', 'flash-common.ps1'})
    assert {p.name for p in package.glob('Flash-*')} == {
        'Flash-Mac.command' if mac else 'Flash-Windows.bat'}
    assert ('macos_arm64_backend' in metadata) == mac
    assert ('macos_intel_backend' in metadata) == mac
    if mac:
        for subdir, key, arch in [('macos-arm64', 'macos_arm64_backend', 'arm64'),
                                  ('macos', 'macos_intel_backend', 'x86_64')]:
            runtime = package / 'tools' / subdir
            info = json.loads((runtime / 'native-info.json').read_text())
            assert info == metadata[key] and info['machine'] == arch
            assert (runtime / '_internal').is_dir()
            assert digest(runtime / 'BLFlashCommand-macos') == info['executable_sha256']
        for key in ('backend', 'source_sha256', 'python'):
            assert metadata['macos_arm64_backend'][key] == metadata['macos_intel_backend'][key]
    actual = {p.relative_to(package).as_posix(): digest(p)
              for name in ('app', 'firmware', 'tools')
              for p in (package / name).rglob('*') if p.is_file()}
    expected = {}
    for line in (package / 'asset-hashes.txt').read_text().splitlines():
        checksum, name = line.split('  ', 1)
        assert name not in expected, name
        expected[name] = checksum
    assert actual == expected, 'manifest must cover every runtime asset exactly'
    assert actual['firmware/ds5dongle-lctech616-hs.bin'] == metadata['firmware_sha256']
    assert (package / 'firmware/version.txt').read_text().strip() == metadata['version']
    usage = (package / '使用说明.txt').read_text(encoding='utf-8-sig')
    assert metadata['version'] in usage and '@' not in usage
    assert ('Flash-Windows.bat' not in usage) if mac else ('Flash-Mac.command' not in usage)
    assert 'erase = 1' in (package / 'firmware/flash_prog_cfg.ini').read_text()
    print(f'PASS: {platform} isolated layout, instructions, firmware identity and complete manifest')
    return platform


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', type=Path, required=True)
    parser.add_argument('--pwsh', type=Path, help='Required for Windows package tests; otherwise found on PATH')
    args = parser.parse_args()
    package = args.package.resolve()
    platform = test_layout(package)
    pwsh = args.pwsh or shutil.which('pwsh')
    if platform == 'macos':
        for hardware, process, expected in [('1','arm64','macos-arm64'), ('1','x86_64','macos-arm64'),
                                            ('0','x86_64','macos'), ('','arm64','macos-arm64')]:
            actual = subprocess.check_output(['/bin/bash','-c','source "$1"; mac_tool_subdir "$2" "$3"',
                                              'test',str(package/'app/flash-common.sh'),hardware,process],text=True).strip()
            assert actual == expected,(hardware,process,actual)
        print('PASS: Apple Silicon / Intel selection, including a translated parent shell')
        subprocess.run(['/bin/bash', str(package / 'Flash-Mac.command'), '--check'], check=True, timeout=30)
    else:
        if not pwsh: parser.error('Windows package tests require --pwsh or pwsh on PATH')
        test_powershell(package, Path(pwsh).resolve())
    for outcome, count in [('success',1), ('exit-error',1), ('no-verification',1), ('wrong-hash',1),
                           ('corrupt',1), ('success',0), ('success',2)]:
        run_case(package, pwsh, platform, outcome, count)
    if platform == 'macos':
        run_case(package, pwsh, platform, 'success', 1, external_logs=True)
    print('PASS: no real device was opened or flashed by these tests')


if __name__ == '__main__':
    main()
