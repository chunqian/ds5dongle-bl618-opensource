#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Re-sign a verified, prebuilt flashing package on another Mac; no SDK needed.

Only local Developer ID Application identities are accepted by the CLI.
This script never imports/exports private keys or opens a serial port.
"""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import platform
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile

HERE = Path(__file__).resolve().parent
MAGIC = {b'\xfe\xed\xfa\xce', b'\xce\xfa\xed\xfe', b'\xfe\xed\xfa\xcf',
         b'\xcf\xfa\xed\xfe', b'\xca\xfe\xba\xbe', b'\xbe\xba\xfe\xca',
         b'\xca\xfe\xba\xbf', b'\xbf\xba\xfe\xca'}
INTEL_ENTITLEMENTS = {'com.apple.security.cs.disable-library-validation': True}
ASSET_DIRS = ('app', 'firmware', 'tools')


def sha(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def save_json(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def run(argv, *, cwd=None, timeout=180, log=None):
    result = subprocess.run([str(x) for x in argv], cwd=cwd, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    if log:
        log.write_text(result.stdout + result.stderr, encoding='utf-8')
    if result.returncode:
        raise RuntimeError(f"命令失败（{result.returncode}）：{argv[0]}\n"
                           + result.stdout + result.stderr)
    return result.stdout


def identities():
    output = run(['/usr/bin/security', 'find-identity', '-v', '-p', 'codesigning'])
    found = re.findall(r'^\s*\d+\) ([0-9A-Fa-f]{40}) "(Developer ID Application: [^"\n]+)"',
                       output, re.MULTILINE)
    return [{'sha1': digest.upper(), 'name': name} for digest, name in found]


def choose_identity(available, requested=None, interactive=False):
    if requested:
        available = [item for item in available
                     if requested.upper() == item['sha1'] or requested == item['name']]
    if not available:
        raise RuntimeError('未找到可用的本地 Developer ID Application 证书及私钥。\n'
                           'Apple Development、Apple Distribution 和 Managed 云托管证书不能用于此脚本。\n'
                           '需要 Account Holder 创建本地 Developer ID Application，或导入含私钥的 .p12。')
    if len(available) == 1:
        return available[0]
    for number, item in enumerate(available, 1):
        print(f"  {number}. {item['name']} [{item['sha1']}]")
    if not interactive:
        raise RuntimeError('找到多张证书，请通过 --identity 指定上述 SHA-1。')
    selected = input('请选择证书编号：').strip()
    if not selected.isdigit() or not 1 <= int(selected) <= len(available):
        raise RuntimeError('无效的证书编号。')
    return available[int(selected) - 1]


def load_input():
    config = json.loads((HERE / 'signing-input.json').read_text(encoding='utf-8'))
    name = config['archive']
    if Path(name).name != name or not name.endswith('.zip'):
        raise RuntimeError('签名材料中的压缩包名称无效。')
    archive = HERE / 'payload' / name
    if sha(archive) != config['archive_sha256']:
        raise RuntimeError('原始烧录包 SHA-256 不符，请重新复制完整签名材料。')
    return config, archive


def extract_package(archive, destination):
    """Reject links/traversal/duplicate paths before writing any archive entries."""
    with zipfile.ZipFile(archive) as zipped:
        members, roots, seen = zipped.infolist(), set(), set()
        for item in members:
            path = PurePosixPath(item.filename)
            mode = item.external_attr >> 16
            if (path.is_absolute() or '..' in path.parts or not path.parts
                    or '\\' in item.filename or '\n' in item.filename or '\r' in item.filename
                    or str(path) in seen or stat.S_ISLNK(mode)
                    or stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)):
                raise RuntimeError(f'压缩包路径或文件类型无效：{item.filename!r}')
            seen.add(str(path))
            roots.add(path.parts[0])
        if len(roots) != 1:
            raise RuntimeError('压缩包必须只包含一个顶层烧录目录。')
        for item in members:
            path = destination / item.filename
            if item.is_dir():
                path.mkdir(parents=True, exist_ok=True)
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            with zipped.open(item) as source, path.open('wb') as target:
                shutil.copyfileobj(source, target)
            # Preserve only executable bits, never setuid/setgid or arbitrary permissions.
            path.chmod(0o755 if (item.external_attr >> 16) & 0o111 else 0o644)
    return destination / roots.pop()


def asset_files(package):
    return sorted(path for folder in ASSET_DIRS for path in (package / folder).rglob('*')
                  if path.is_file())


def verify_package(package, expected_firmware):
    recorded = {}
    for line in (package / 'asset-hashes.txt').read_text(encoding='ascii').splitlines():
        match = re.fullmatch(r'([0-9a-f]{64})  (.+)', line)
        if not match or match[2] in recorded:
            raise RuntimeError('烧录包的文件校验清单无效。')
        recorded[match[2]] = match[1]
    actual = {path.relative_to(package).as_posix(): sha(path) for path in asset_files(package)}
    if actual != recorded:
        raise RuntimeError('烧录包文件缺失、被修改，或存在未列入校验清单的文件。')
    if sha(package / 'firmware/ds5dongle-lctech616-hs.bin') != expected_firmware:
        raise RuntimeError('固件 SHA-256 与签名材料不符。')


def macho_files(package):
    result = []
    for path in asset_files(package):
        with path.open('rb') as stream:
            if stream.read(4) in MAGIC:
                result.append(path)
    # Sign libraries first, then the executables which load them; no --deep signing.
    return sorted(result, key=lambda p: (p.name in ('BLFlashCommand-macos', 'list-ports-macos'), str(p)))


def sign_file(path, identity, entitlements=None):
    command = ['/usr/bin/codesign', '--force', '--sign', identity, '--timestamp', '--options', 'runtime']
    if entitlements:
        command += ['--entitlements', entitlements]
    run(command + [path])


def verify_signature(path, expected_name, runtime=True):
    run(['/usr/bin/codesign', '--verify', '--strict', '--all-architectures', path])
    architectures = run(['/usr/bin/lipo', '-archs', path]).strip().split() if runtime else [None]
    team = None
    for arch in architectures:
        command = ['/usr/bin/codesign', '-d', '--verbose=4']
        if arch:
            command += ['--arch', arch]
        details = subprocess.run([*command, str(path)], text=True, capture_output=True, check=True)
        details = details.stdout + details.stderr
        if ('Authority=' + expected_name + '\n') not in details or not re.search(r'^Timestamp=', details, re.M):
            raise RuntimeError(f'签名身份或安全时间戳验证失败：{path}')
        if runtime and not re.search(r'^CodeDirectory .*flags=.*\bruntime\b', details, re.M):
            raise RuntimeError(f'Hardened Runtime 未开启：{path} ({arch})')
        current_team = re.search(r'^TeamIdentifier=(\w+)$', details, re.M)
        if not current_team or current_team[1] == 'not' or (team and team != current_team[1]):
            raise RuntimeError(f'Team ID 验证失败：{path}')
        team = current_team[1]
    return team


def sign_package(package, identity, output):
    files = macho_files(package)
    required = {'app/list-ports-macos', 'tools/macos/BLFlashCommand-macos',
                'tools/macos-arm64/BLFlashCommand-macos'}
    if not required.issubset({p.relative_to(package).as_posix() for p in files}):
        raise RuntimeError('缺少 Mac 烧录程序或端口检测程序。')
    entitlements = output / 'intel-runtime-entitlements.plist'
    entitlements.write_bytes(plistlib.dumps(INTEL_ENTITLEMENTS))
    records = []
    for path in files:
        relative = path.relative_to(package).as_posix()
        print('签名：' + relative, flush=True)
        # The vendor Intel onefile embeds Python libraries with other Team IDs.
        # Only this executable needs the exception; ARM64 runtime libraries are re-signed below.
        exception = entitlements if relative == 'tools/macos/BLFlashCommand-macos' else None
        sign_file(path, identity['sha1'], exception)
        team = verify_signature(path, identity['name'])
        records.append({'path': relative, 'sha256': sha(path), 'authority': identity['name'],
                        'team_identifier': team,
                        'entitlements': INTEL_ENTITLEMENTS if exception else {}})
    signing = {'identity': identity['sha1'], 'files': records, 'hardened_runtime': True,
               'secure_timestamp': True, 'notarized': None,
               'notarization_note': 'See the external release-result.json for the final DMG notarization status.'}
    save_json(package / 'signing-info.json', signing)
    native_path = package / 'tools/macos-arm64/native-info.json'
    native = json.loads(native_path.read_text())
    native.setdefault('unsigned_executable_sha256', native['executable_sha256'])
    native['executable_sha256'] = sha(package / 'tools/macos-arm64/BLFlashCommand-macos')
    native['code_signing_identity'] = identity['sha1']
    save_json(native_path, native)
    metadata_path = package / 'package-info.json'
    metadata = json.loads(metadata_path.read_text())
    metadata['macos_arm64_backend'] = native
    metadata['macos_code_signing'] = signing
    save_json(metadata_path, metadata)
    # Keep embedded sources consistent with the distribution process actually used.
    shutil.copyfile(Path(__file__), package / 'source/easy_flash/sign_macos_release.py')
    usage = package / '使用说明.txt'
    text = usage.read_text(encoding='utf-8-sig').replace(
        'Apple Development 开发签名不等于 Developer ID 分发签名，本包未经过苹果公证。',
        '本包工具使用 Developer ID Application 分发签名。DMG 的公证结果见外部 release-result.json。')
    usage.write_text(text, encoding='utf-8-sig')
    notice = package / 'NOTICE.txt'
    text = notice.read_text().replace(
        'The bundled tools are not notarized.',
        'Developer ID signatures are applied before DMG creation; final notarization status is in the external release-result.json.')
    text += ('\nIntel vendor onefile: disable-library-validation is required for its embedded Python libraries.\n'
             'ARM64 runtime libraries are signed with the same identity without this entitlement.\n')
    notice.write_text(text, encoding='utf-8')
    (package / 'asset-hashes.txt').write_text(''.join(
        f'{sha(path)}  {path.relative_to(package).as_posix()}\n' for path in asset_files(package)), encoding='ascii')
    return signing


def runtime_check(package, output):
    # --check reads only the port inventory; --build below only generates an offline image.
    run(['/bin/bash', package / 'Flash-Mac.command', '--check'], log=output / 'runtime-check.log')
    hardware = subprocess.run(['/usr/sbin/sysctl', '-n', 'hw.optional.arm64'],
                              capture_output=True, text=True)
    arm = hardware.stdout.strip() == '1' or platform.machine() == 'arm64'
    backend = 'macos-arm64' if arm else 'macos'
    with tempfile.TemporaryDirectory(prefix='ds5-signed-offline-') as temporary:
        stage = Path(temporary)
        shutil.copytree(package / 'tools' / backend, stage / 'tool')
        shutil.copytree(package / 'tools/common', stage / 'tool', dirs_exist_ok=True)
        shutil.copytree(package / 'firmware', stage / 'firmware')
        run([stage / 'tool/BLFlashCommand-macos', '--chipname=bl616',
             '--config=flash_prog_cfg.ini', '--build'], cwd=stage / 'firmware',
            log=output / 'offline-build.log')
        images = list(stage.rglob('whole_flash_data.bin'))
        if len(images) != 1:
            raise RuntimeError('离线镜像验证失败：未找到唯一的 whole_flash_data.bin。')
        return {'backend': backend, 'image_sha256': sha(images[0]), 'device_flashed': False}


def make_dmg(package, identity, output):
    # The flasher saves logs beside itself, so it must be copied off the read-only DMG.
    (package.parent / '先复制到下载目录.txt').write_text(
        '请把整个 DS5Dongle 文件夹复制到本机“下载”或“桌面”，再双击其中的 Flash-Mac.command。\n'
        '不要直接在只读磁盘映像中烧录。\n', encoding='utf-8')
    dmg = output / (package.name + '.dmg')
    run(['/usr/bin/hdiutil', 'create', '-srcfolder', package.parent, '-volname', 'DS5Dongle R6',
         '-format', 'UDZO', '-fs', 'HFS+', dmg], timeout=300, log=output / 'dmg-build.log')
    run(['/usr/bin/codesign', '--force', '--sign', identity['sha1'], '--timestamp', dmg])
    verify_signature(dmg, identity['name'], runtime=False)
    run(['/usr/bin/hdiutil', 'verify', dmg])
    return dmg


def ensure_profile(profile, identity, interactive):
    run(['/usr/bin/xcrun', '--find', 'notarytool'])
    try:
        run(['/usr/bin/xcrun', 'notarytool', 'history', '--keychain-profile', profile,
             '--output-format', 'json'])
    except RuntimeError as error:
        if not interactive or 'No Keychain password item found' not in str(error):
            raise RuntimeError(f'公证凭据不可用：{profile}\n{error}\n'
                               '请运行：xcrun notarytool store-credentials "' + profile + '"') from error
        print('还没有公证凭据。请输入 Apple 账号，随后在安全提示中输入 App 专用密码。')
        account = input('Apple 账号邮箱（留空取消）：').strip()
        if not account:
            raise RuntimeError('未配置公证凭据。可重新运行并选择“只签名”。')
        team = re.search(r'\(([A-Z0-9]{10})\)$', identity['name'])
        if not team:
            raise RuntimeError('无法从证书名称取得 Team ID，请手动配置公证凭据。')
        # Inherit the terminal: notarytool securely prompts without recording the password.
        subprocess.run(['/usr/bin/xcrun', 'notarytool', 'store-credentials', profile,
                        '--apple-id', account, '--team-id', team[1]], check=True)


def finish_notarization(output, state, profile):
    dmg = output / state['dmg']
    if Path(state['dmg']).name != state['dmg']:
        raise RuntimeError('公证记录中的 DMG 路径无效。')
    if sha(dmg) != state['dmg_sha256']:
        raise RuntimeError('DMG 已改变，不能沿用旧公证记录；请重新签名。')
    state['profile'] = profile
    if not state.get('submission_id'):
        print('正在上传 DMG 到 Apple 公证服务……', flush=True)
        reply = run(['/usr/bin/xcrun', 'notarytool', 'submit', dmg, '--keychain-profile', profile,
                     '--output-format', 'json', '--no-progress'], timeout=1800,
                    log=output / 'notary-submit.log')
        submitted = json.loads(reply)
        state['submission_id'] = submitted['id']
        state['status'] = 'notarization_pending'
        save_json(output / 'release-result.json', state)
    submission = state['submission_id']
    print(f'公证编号：{submission}\n等待 Apple 结果；记录已保存，可中断后用 --resume 继续。', flush=True)
    reply = run(['/usr/bin/xcrun', 'notarytool', 'wait', submission, '--keychain-profile', profile,
                 '--timeout', '30m', '--output-format', 'json', '--no-progress'], timeout=1860,
                log=output / 'notary-wait.log')
    result = json.loads(reply)
    state['apple_status'] = result.get('status')
    save_json(output / 'release-result.json', state)
    run(['/usr/bin/xcrun', 'notarytool', 'log', submission, '--keychain-profile', profile,
         output / 'notary-log.json'])
    if result.get('status') != 'Accepted':
        raise RuntimeError('Apple 公证未通过：' + str(result.get('status')) + '\n请查看 notary-log.json。')
    state['status'] = 'accepted_not_stapled'
    save_json(output / 'release-result.json', state)
    run(['/usr/bin/xcrun', 'stapler', 'staple', dmg], log=output / 'staple.log')
    # Stapling changes the DMG hash; persist it before any subsequent validation can fail.
    state['dmg_sha256'] = sha(dmg)
    state['status'] = 'stapled'
    save_json(output / 'release-result.json', state)
    run(['/usr/bin/xcrun', 'stapler', 'validate', dmg], log=output / 'staple-validation.log')
    run(['/usr/sbin/spctl', '--assess', '--type', 'open', '--context', 'context:primary-signature',
         '--verbose=2', dmg], log=output / 'gatekeeper.log')
    state['status'] = 'notarized'
    save_json(output / 'release-result.json', state)
    (output / (dmg.name + '.sha256')).write_text(f'{sha(dmg)}  {dmg.name}\n', encoding='ascii')
    print('公证通过，票据已附加，Gatekeeper 验证通过。\n分发文件：' + str(dmg))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--check', action='store_true', help='只检查材料、身份；不签名、不上传')
    modes.add_argument('--sign-only', action='store_true', help='只签名并生成 DMG，不上传 Apple')
    modes.add_argument('--notarize', action='store_true', help='签名、上传公证并附加票据')
    modes.add_argument('--resume', type=Path, help='继续已有输出目录的公证，不重新签名')
    parser.add_argument('--identity', help='本地 Developer ID Application 的 SHA-1 或完整名称')
    parser.add_argument('--profile', help='钥匙串中的公证凭据名称；默认 DS5DongleNotary')
    args = parser.parse_args()
    requested_profile = args.profile
    args.profile = args.profile or 'DS5DongleNotary'
    if platform.system() != 'Darwin':
        raise RuntimeError('请在安装了 Xcode 的 Mac 上运行。')
    interactive = sys.stdin.isatty()
    if args.resume:
        output = args.resume.resolve()
        state = json.loads((output / 'release-result.json').read_text())
        if not state.get('dmg'):
            raise RuntimeError('该输出目录尚未完成签名打包，请重新运行签名脚本。')
        profile = requested_profile or state.get('profile', args.profile)
        ensure_profile(profile, state['identity'], interactive)
        finish_notarization(output, state, profile)
        return
    config, archive = load_input()
    identity = choose_identity(identities(), args.identity, interactive)
    print('使用证书：' + identity['name'])
    print('固件：' + config['version'])
    if not any((args.check, args.sign_only, args.notarize)):
        if not interactive:
            raise RuntimeError('请指定 --check、--sign-only 或 --notarize。')
        answer = input('1. 签名并提交 Apple 公证（默认）\n2. 只签名\n请选择：').strip()
        if answer not in ('', '1', '2'):
            raise RuntimeError('无效的选项。')
        args.notarize = answer != '2'
    if args.notarize:
        ensure_profile(args.profile, identity, interactive)
    if args.check:
        with tempfile.TemporaryDirectory(prefix='ds5-sign-check-') as temporary:
            package = extract_package(archive, Path(temporary))
            verify_package(package, config['firmware_sha256'])
            print(f'材料和本地签名身份检查通过：{len(macho_files(package))} 个 Mach-O 文件。未签名、未上传。')
        return
    output_root = HERE / 'output'
    output_root.mkdir(exist_ok=True)
    output = Path(tempfile.mkdtemp(prefix='release-', dir=output_root))
    print('结果和日志：' + str(output), flush=True)
    state = {'status': 'preparing', 'identity': identity, 'version': config['version'],
             'profile': args.profile, 'input_sha256': config['archive_sha256']}
    save_json(output / 'release-result.json', state)
    try:
        package = extract_package(archive, output / 'contents')
        verify_package(package, config['firmware_sha256'])
        package = package.rename(package.with_name(package.name.removesuffix('-Signed') + '-DeveloperID'))
        sign_package(package, identity, output)
        verify_package(package, config['firmware_sha256'])
        state['runtime_check'] = runtime_check(package, output)
        if state['runtime_check']['image_sha256'] != config['offline_image_sha256']:
            raise RuntimeError('签名后生成的离线镜像与已验证镜像不一致。')
        dmg = make_dmg(package, identity, output)
        state.update(status='signed_not_notarized', dmg=dmg.name, dmg_sha256=sha(dmg))
        save_json(output / 'release-result.json', state)
        (output / (dmg.name + '.sha256')).write_text(f'{sha(dmg)}  {dmg.name}\n', encoding='ascii')
        if args.notarize:
            finish_notarization(output, state, args.profile)
        else:
            print('签名和离线验证完成，尚未公证。\n文件：' + str(dmg))
            print('以后可运行：./Sign-Mac.command --resume "' + str(output) + '"')
    except BaseException as error:
        state['last_error'] = str(error) or type(error).__name__
        save_json(output / 'release-result.json', state)
        print('未完成；已保留日志：' + str(output), file=sys.stderr)
        raise


if __name__ == '__main__':
    try:
        main()
    except (Exception, KeyboardInterrupt) as error:
        print('\n失败：' + (str(error) or '用户中断'), file=sys.stderr)
        sys.exit(1)
