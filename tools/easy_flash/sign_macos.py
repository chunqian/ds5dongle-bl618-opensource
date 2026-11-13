"""Sign a prepared macOS flashing kit on the certificate owner's Mac.

Uses only Python's standard library and Apple command line tools. Private keys
stay in Keychain; notarization uses a named notarytool Keychain profile.
"""
from pathlib import Path
import argparse
import hashlib
import json
import plistlib
import platform
import re
import shutil
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
MAGICS = {bytes.fromhex(x) for x in ('feedface', 'cefaedfe', 'feedfacf',
                                   'cffaedfe', 'cafebabe', 'bebafeca',
                                   'cafebabf', 'bfbafeca')}


def run(*args):
    return subprocess.run([str(a) for a in args], check=True, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def macho_files(root):
    result = []
    for path in root.rglob('*'):
        if path.is_symlink():
            raise ValueError(f'Unexpected symlink in prepared payload: {path}')
        if path.is_file():
            with path.open('rb') as stream:
                if stream.read(4) in MAGICS:
                    result.append(path)
    return sorted(result, key=lambda p: (-len(p.parts), str(p)))


def runtime_assets(root):
    return {p.relative_to(root).as_posix(): sha(p)
            for name in ('app', 'tools', 'firmware')
            for p in (root / name).rglob('*') if p.is_file()}


def verify_package(root):
    expected = {}
    for line in (root / 'asset-hashes.txt').read_text().splitlines():
        checksum, name = line.split('  ', 1)
        if name in expected:
            raise ValueError('Duplicate manifest entry: ' + name)
        expected[name] = checksum
    if expected != runtime_assets(root):
        raise ValueError('Input package failed complete asset verification.')
    info = json.loads((root / 'package-info.json').read_text())
    if info['platform'] != 'macos':
        raise ValueError('Only a macOS package can be signed by this tool.')
    if sha(root / 'firmware/ds5dongle-lctech616-hs.bin') != info['firmware_sha256']:
        raise ValueError('Firmware identity mismatch.')
    runtimes = []
    for subdir, key, arch in (('macos-arm64', 'macos_arm64_backend', 'arm64'),
                              ('macos', 'macos_intel_backend', 'x86_64')):
        native = root / 'tools' / subdir
        metadata_path = native / 'native-info.json'
        if not metadata_path.is_file() or not (native / '_internal').is_dir():
            raise ValueError('Both Mac runtimes must be onedir builds; rebuild the macOS package first.')
        runtime = json.loads(metadata_path.read_text())
        if runtime['machine'] != arch or info.get(key) != runtime:
            raise ValueError('Native runtime metadata mismatch: ' + subdir)
        if sha(native / 'BLFlashCommand-macos') != runtime['executable_sha256']:
            raise ValueError('Native runtime hash mismatch: ' + subdir)
        for path in macho_files(native):
            if arch not in run('xcrun', 'lipo', '-archs', path).stdout.split():
                raise ValueError('Incompatible binary: ' + str(path))
        runtimes.append(runtime)
    for key in ('backend', 'source_sha256', 'python'):
        if runtimes[0][key] != runtimes[1][key]:
            raise ValueError('Mac runtimes must use the same ' + key)
    return info


def developer_id(identity=None):
    output = run('security', 'find-identity', '-v', '-p', 'codesigning').stdout
    choices = re.findall(r'\b([0-9A-F]{40}) "(Developer ID Application: [^"]+)"', output)
    selected = [(digest, name) for digest, name in choices
                if identity is None or identity.upper() == digest or identity == name]
    if len(selected) != 1:
        available = '\n'.join(f'  {digest} {name}' for digest, name in choices)
        raise ValueError('Select exactly one valid Developer ID Application identity with --identity.\n'
                         'Apple Development and ad-hoc identities cannot be used for distribution.\n' + available)
    return selected[0]


def prepare_app(package, app):
    metadata = verify_package(package)
    resources = app / 'Contents/Resources'
    resources.mkdir(parents=True)
    payload = resources / 'payload'
    shutil.copytree(package, payload)
    # A previously generated package can have old wrappers; always use this kit's
    # matching launcher and log handling to keep the signed resources immutable.
    for name in ('flash-mac.sh', 'flash-common.sh'):
        shutil.copyfile(HERE / name, payload / 'app' / name)
    shutil.copyfile(HERE / 'Flash-Mac.command', payload / 'Flash-Mac.command')
    (payload / 'Flash-Mac.command').chmod(0o755)
    shutil.copyfile(HERE / 'Run-Mac-App.command', resources / 'Run.command')
    (resources / 'Run.command').chmod(0o755)
    executable = app / 'Contents/MacOS/DS5DongleFlash'
    executable.parent.mkdir()
    run('xcrun', 'clang', '-arch', 'arm64', '-arch', 'x86_64',
        '-mmacosx-version-min=11.0', '-Os', '-Wall', '-Wextra', '-Werror',
        '-fobjc-arc', '-framework', 'Cocoa', HERE / 'mac_launcher.m', '-o', executable)
    executable.chmod(0o755)
    plist = {'CFBundleIdentifier': 'com.ds5dongle.flash', 'CFBundleName': 'DS5Dongle Flash',
             'CFBundleDisplayName': 'DS5Dongle Flash', 'CFBundleExecutable': 'DS5DongleFlash',
             'CFBundlePackageType': 'APPL', 'CFBundleShortVersionString': '1.0',
             'CFBundleVersion': '1', 'LSMinimumSystemVersion': '11.0', 'LSUIElement': True,
             'NSHighResolutionCapable': True}
    (app / 'Contents/Info.plist').write_bytes(plistlib.dumps(plist))
    return payload, metadata


def sign_app(app, payload, metadata, identity, name):
    binaries = macho_files(payload)
    if len(binaries) < 13:
        raise ValueError('Prepared bundle is missing expected native dependencies.')
    print(f'正在签名 {len(binaries)} 个内部程序/动态库和 App…', flush=True)
    for path in binaries:
        run('codesign', '--force', '--sign', identity, '--timestamp', '--options', 'runtime', path)
        run('codesign', '--verify', '--strict', '--all-architectures', path)
    for subdir, key in (('macos-arm64', 'macos_arm64_backend'), ('macos', 'macos_intel_backend')):
        path = payload / 'tools' / subdir / 'native-info.json'
        info = json.loads(path.read_text())
        info['unsigned_executable_sha256'] = info['executable_sha256']
        info['executable_sha256'] = sha(path.parent / 'BLFlashCommand-macos')
        info['code_signing_identity'] = identity
        write_json(path, info)
        metadata[key] = info
    metadata['code_signing'] = {'identity_sha1': identity, 'name': name,
                               'hardened_runtime': True, 'secure_timestamp': True}
    write_json(payload / 'package-info.json', metadata)
    usage = payload / '使用说明.txt'
    text = usage.read_text(encoding='utf-8-sig')
    text = text.replace('本包尚未完成 Developer ID 签名及 Apple 公证。',
                        '本 App 已使用 Developer ID 签名；公证结果见分发包旁的 receipt.json。')
    text = text.replace('双击 Flash-Mac.command。', '双击 DS5Dongle Flash.app。')
    text = text.replace('同一目录的 logs 文件夹内', '~/Library/Logs/DS5Dongle-Flash 内')
    usage.write_text(text, encoding='utf-8-sig')
    notice = payload / 'NOTICE.txt'
    text = notice.read_text().replace('SDK FlashCube binary is copied unchanged.',
                                     'Both Mac architectures use onedir bflb-flash-command 1.0.0 builds.')
    text = text.replace('This package has not completed Developer ID signing or Apple notarization.',
                        'Developer ID signed. Notarization status is recorded outside the sealed app in receipt.json.')
    notice.write_text(text, encoding='utf-8')
    (payload / 'asset-hashes.txt').write_text(''.join(
        f'{value}  {key}\n' for key, value in sorted(runtime_assets(payload).items())), encoding='ascii')
    run('codesign', '--force', '--sign', identity, '--timestamp', '--options', 'runtime', app)
    run('codesign', '--verify', '--deep', '--strict', '--all-architectures', app)
    verify_package(payload)
    # Check actual hardened library loading before uploading; never open a device.
    subdir = 'macos-arm64' if platform.machine() == 'arm64' else 'macos'
    info = json.loads(run(payload / 'tools' / subdir / 'BLFlashCommand-macos', '--runtime-info').stdout)
    if info['machine'] != platform.machine():
        raise ValueError('Signed native runtime architecture mismatch.')


def archive_app(app, target):
    run('ditto', '-c', '-k', '--keepParent', '--sequesterRsrc', app, target)


def notarize(app, work, profile):
    archive = work / 'notary-upload.zip'
    archive_app(app, archive)
    print('正在提交 Apple 公证并等待结果（最长 20 分钟）…', flush=True)
    command = ['xcrun', 'notarytool', 'submit', str(archive), '--keychain-profile', profile,
               '--wait', '--timeout', '20m', '--output-format', 'json']
    result = subprocess.run(command, text=True, capture_output=True)
    (work / 'notary-result.json').write_text(result.stdout)
    (work / 'notary-stderr.txt').write_text(result.stderr)
    try:
        receipt = json.loads(result.stdout)
    except ValueError:
        raise ValueError(f'Notarization did not return a receipt. See {work}/notary-stderr.txt')
    submission = receipt.get('id')
    if submission:
        log = subprocess.run(['xcrun', 'notarytool', 'log', submission,
                              '--keychain-profile', profile, str(work / 'notary-log.json')],
                             text=True, capture_output=True)
        (work / 'notary-log-fetch.txt').write_text(log.stdout + log.stderr)
    if result.returncode or receipt.get('status') != 'Accepted':
        raise ValueError(f'Notarization not accepted; no release ZIP produced. Receipt: {work}/notary-result.json')
    run('xcrun', 'stapler', 'staple', app)
    run('xcrun', 'stapler', 'validate', app)
    run('codesign', '--verify', '--deep', '--strict', '--all-architectures', app)
    assessment = run('spctl', '--assess', '--type', 'execute', '--verbose=2', app)
    (work / 'gatekeeper.txt').write_text(assessment.stdout + assessment.stderr)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', type=Path, default=HERE / 'input/package')
    parser.add_argument('--output', type=Path, default=HERE / 'output')
    parser.add_argument('--identity', help='Developer ID Application certificate SHA-1 or full name')
    parser.add_argument('--keychain-profile', help='Previously stored notarytool credential profile name')
    parser.add_argument('--sign-only', action='store_true', help='Explicitly skip notarization; output is marked Unnotarized')
    parser.add_argument('--check', action='store_true', help='Validate inputs and build launcher without signing, uploading or flashing')
    parser.add_argument('--interactive', action='store_true')
    args = parser.parse_args()
    if sys.platform != 'darwin':
        parser.error('Run on macOS with Xcode Command Line Tools installed.')
    for source in (args.package.resolve(),):
        if args.output.resolve() == source or source in args.output.resolve().parents:
            parser.error('--output must be outside the input package.')
    kit_manifest = HERE / 'kit-hashes.txt'
    if kit_manifest.exists():
        for line in kit_manifest.read_text(encoding='utf-8').splitlines():
            checksum, name = line.split('  ', 1)
            relative = Path(name)
            if relative.is_absolute() or '..' in relative.parts or sha(HERE / relative) != checksum:
                raise ValueError('Signing kit is incomplete or modified: ' + name)
    verify_package(args.package)
    if args.check:
        with tempfile.TemporaryDirectory(prefix='ds5-sign-check-') as directory:
            app = Path(directory) / 'DS5Dongle Flash.app'
            payload, _ = prepare_app(args.package, app)
            print(f'PASS: prepared universal launcher and {len(macho_files(payload))} payload Mach-O files.')
        return
    if args.sign_only and args.keychain_profile:
        parser.error('--sign-only cannot be combined with --keychain-profile')
    if args.interactive and not args.identity:
        print(run('security', 'find-identity', '-v', '-p', 'codesigning').stdout)
        args.identity = input('Developer ID Application 证书 SHA-1（只有一个时可留空）：').strip() or None
    identity, name = developer_id(args.identity)
    if args.interactive and not args.sign_only and not args.keychain_profile:
        args.keychain_profile = input('notarytool 钥匙串配置名：').strip()
    if not args.sign_only and not args.keychain_profile:
        parser.error('Provide --keychain-profile, or explicitly choose --sign-only.')
    args.output.mkdir(parents=True, exist_ok=True)
    # A unique work directory preserves failed submissions and prevents replacing
    # an existing release or modifying the input bundle.
    work = Path(tempfile.mkdtemp(prefix='sign-', dir=args.output.resolve()))
    print('工作目录：', work, flush=True)
    app = work / 'DS5Dongle Flash.app'
    payload, metadata = prepare_app(args.package, app)
    sign_app(app, payload, metadata, identity, name)
    receipt = {'status': 'Not submitted'} if args.sign_only else notarize(app, work, args.keychain_profile)
    suffix = 'Signed-Unnotarized' if args.sign_only else 'Signed-Notarized'
    release = work / 'release'
    release.mkdir()
    version = re.sub(r'[^A-Za-z0-9._-]+', '-', metadata['version'])
    archive = release / ('DS5Dongle-' + version + '-Mac-' + suffix + '.zip')
    archive_app(app, archive)
    archive.with_suffix('.zip.sha256').write_text(f'{sha(archive)}  {archive.name}\n')
    write_json(release / 'receipt.json', {'notarization': receipt, 'version': metadata['version'],
                                       'signing_identity': name, 'zip_sha256': sha(archive),
                                       'firmware_sha256': metadata['firmware_sha256']})
    print('完成：', archive)
    print('公证状态：', receipt['status'])


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print(f'失败：{error}', file=sys.stderr)
        if isinstance(error, subprocess.CalledProcessError):
            print(error.stdout or '', file=sys.stderr)
            print(error.stderr or '', file=sys.stderr)
        raise SystemExit(1)
