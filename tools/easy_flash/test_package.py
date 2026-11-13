"""Exercise launchers with a fake flasher; never opens or writes a real device.

python3 tools/easy_flash/test_package.py --package /path/to/extracted/package [--pwsh /path/to/pwsh]
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

from sign_macos_release import verify_package


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
    if platform == 'macos':
        parts.append('tools/macos-arm64')
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
    parser.add_argument('--pwsh', type=Path, help='Required for Windows launcher tests only')
    args = parser.parse_args()
    package = args.package.resolve()
    pwsh = args.pwsh.resolve() if args.pwsh else None
    metadata = json.loads((package / 'package-info.json').read_text())
    platform = metadata.get('platform')
    platforms = [platform] if platform else ['macos', 'windows']
    assert all(p in ('macos', 'windows') for p in platforms)
    if 'windows' in platforms and not pwsh:
        parser.error('--pwsh is required for Windows launcher tests')
    verify_package(package, metadata['firmware_sha256'])
    assert (package / 'firmware/version.txt').read_text().strip() == metadata['version']
    if platform:
        expected_app = {'flash-mac.sh', 'flash-common.sh', 'list-ports-macos'} if platform == 'macos' else {'flash-windows.ps1', 'flash-common.ps1'}
        expected_tools = {'common', 'macos', 'macos-arm64'} if platform == 'macos' else {'common', 'windows'}
        assert {p.name for p in (package / 'app').iterdir()} == expected_app
        assert {p.name for p in (package / 'tools').iterdir()} == expected_tools
        assert (package / 'Flash-Mac.command').exists() == (platform == 'macos')
        assert (package / 'Flash-Windows.bat').exists() == (platform == 'windows')
        usage = (package / '使用说明.txt').read_text(encoding='utf-8-sig')
        assert ('Flash-Mac.command' in usage) == (platform == 'macos')
        assert ('Flash-Windows.bat' in usage) == (platform == 'windows')
        if platform == 'windows':
            assert not (package / 'signing-info.json').exists()
            assert not (package / 'licenses/macOS-native').exists()
            assert not (package / 'source/native-flash-command-source.tar.gz').exists()
            assert not any(key.startswith('macos') for key in metadata)
            # Windows PE machine type, verified without executing the EXE on Mac.
            exe = (package / 'tools/windows/BLFlashCommand.exe').read_bytes()
            offset = int.from_bytes(exe[0x3c:0x40], 'little')
            assert exe[:2] == b'MZ' and exe[offset:offset + 4] == b'PE\0\0'
            assert int.from_bytes(exe[offset + 4:offset + 6], 'little') == 0x8664
            launcher = (package / 'Flash-Windows.bat').read_bytes()
            assert b'\r\n' in launcher and b'\n' not in launcher.replace(b'\r\n', b'')
        print('PASS: standalone platform layout, firmware and complete asset hashes')
    if (package / 'signing-info.json').exists():
        signing = json.loads((package / 'signing-info.json').read_text())
        actual_macho = set()
        for folder in ('app', 'tools/macos', 'tools/macos-arm64'):
            for path in (package / folder).rglob('*'):
                if path.is_file() and 'Mach-O' in subprocess.check_output(['file', '-b', str(path)], text=True):
                    actual_macho.add(path.relative_to(package).as_posix())
        assert actual_macho == {entry['path'] for entry in signing['files']}
        for entry in signing['files']:
            path = package / entry['path']
            assert digest(path) == entry['sha256'], entry['path']
            subprocess.run(['codesign', '--verify', '--strict', '--all-architectures', str(path)], check=True)
            details = subprocess.check_output(['codesign', '-d', '--verbose=2', str(path)],
                                              stderr=subprocess.STDOUT, text=True)
            assert 'Authority=' + entry['authority'] in details
            assert 'TeamIdentifier=' + entry['team_identifier'] in details
        native = json.loads((package / 'tools/macos-arm64/native-info.json').read_text())
        assert native['executable_sha256'] == digest(package / 'tools/macos-arm64/BLFlashCommand-macos')
        print('PASS: complete Mac signature coverage, identities and signed-file hashes')
    if 'macos' in platforms:
        for hardware, process, expected in [('1','arm64','macos-arm64'), ('1','x86_64','macos-arm64'),
                                            ('0','x86_64','macos'), ('','arm64','macos-arm64')]:
            actual = subprocess.check_output(['/bin/bash','-c','source "$1"; mac_tool_subdir "$2" "$3"',
                                              'test',str(package/'app/flash-common.sh'),hardware,process],text=True).strip()
            assert actual == expected,(hardware,process,actual)
        print('PASS: Apple Silicon / Intel selection, including a translated parent shell')
        subprocess.run(['/bin/bash', str(package / 'Flash-Mac.command'), '--check'], check=True, timeout=30)
    if 'windows' in platforms:
        test_powershell(package, pwsh)
    for platform in platforms:
        for outcome, count in [('success',1), ('exit-error',1), ('no-verification',1), ('wrong-hash',1),
                               ('corrupt',1), ('success',0), ('success',2)]:
            run_case(package, pwsh, platform, outcome, count)
    print('PASS: no real device was opened or flashed by these tests')


if __name__ == '__main__':
    main()
