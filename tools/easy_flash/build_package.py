"""Build separate offline Windows and macOS LCTech BL616 HS flashing packages.

Only the maintainer needs Python (plus Xcode for the Mac package).
Run from the repo: python3 tools/easy_flash/build_package.py --platform all
"""
from pathlib import Path
import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile
import zipfile

REPO = Path(__file__).resolve().parents[2]
TEMPLATES = Path(__file__).resolve().parent
SDK_COMMIT = "b3532953322194233f80436701e7fb6bb4ad8275"
FIRMWARE_SHA = "d8cc0c465e44ef4cd6c7e292368c8f87ccaf39b250050c96998f79d8c6f37b04"
RELEASE = "R6"
VERSION = "LCT616-DS5 3.18H-HidAudio-R6"
NAME = "DS5Dongle-LCTech616-HS-HidAudio-R6-Flash"
# The pinned R6 firmware passed hardware flashing and read-back verification.
FIRMWARE_HARDWARE_TESTED = True


def package_name(platform, signed=False):
    if platform not in ('windows', 'macos'):
        raise ValueError(f'Unsupported package platform: {platform}')
    return NAME + ('-Windows' if platform == 'windows' else '-Mac') + (
        '-Signed' if platform == 'macos' and signed else '')


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args])


def sign_macos_tools(package, identity):
    """Sign the shipped Mach-O files before generating the integrity manifest.

    Sign runtime libraries as well as launchers. Shell scripts remain ordinary
    text files: a .command file is not a signed/notarized application bundle.
    """
    candidates = [package / "app/list-ports-macos", package / "tools/macos/BLFlashCommand-macos"]
    candidates += sorted((package / "tools/macos-arm64").rglob('*'))
    signed = []
    for path in candidates:
        if not path.is_file():
            continue
        kind = subprocess.check_output(["file", "-b", str(path)], text=True)
        if "Mach-O" not in kind:
            continue
        subprocess.run(["codesign", "--force", "--sign", identity,
                        "--timestamp=none", str(path)], check=True, capture_output=True)
        subprocess.run(["codesign", "--verify", "--strict", "--all-architectures", str(path)],
                       check=True, capture_output=True)
        details = subprocess.check_output(["codesign", "-d", "--verbose=2", str(path)],
                                          stderr=subprocess.STDOUT, text=True)
        authorities = [line.removeprefix("Authority=") for line in details.splitlines()
                       if line.startswith("Authority=")]
        if not authorities:
            raise SystemExit(f"Expected a certificate signature, not ad-hoc signing: {path}")
        team = next((line.split('=', 1)[1] for line in details.splitlines()
                     if line.startswith('TeamIdentifier=')), None)
        signed.append({"path": path.relative_to(package).as_posix(), "sha256": sha(path),
                       "authority": authorities[0], "team_identifier": team})
    if len(signed) < 3:
        raise SystemExit("Missing signed Mac executables.")
    result = {"identity": identity, "notarized": False, "files": signed}
    (package / "signing-info.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def build_platform(args, platform, revision):
    sdk, output = args.sdk.resolve(), args.output.resolve()
    firmware = args.firmware_dir.resolve()
    native = args.native_dir.resolve()
    native_info = None
    # Windows builds never read the Mac runtime or invoke Xcode/codesign.
    if platform == 'macos':
        native_info = json.loads((native / 'native-info.json').read_text())
        if native_info['machine'] != 'arm64' or native_info['rosetta'] or native_info['minimum_macos'] != '11.0':
            raise SystemExit('The Apple Silicon runtime must be native ARM64 and compatible with macOS 11.')
        if sha(native / 'runtime/BLFlashCommand-macos') != native_info['executable_sha256']:
            raise SystemExit('Native executable differs from its build metadata.')
    cube = 'tools/bflb_tools/bouffalo_flash_cube'
    with tempfile.TemporaryDirectory(prefix='ds5-package-') as temporary:
        name = package_name(platform, bool(args.sign_identity))
        package = Path(temporary) / name
        for folder in ('app', 'firmware', f'tools/{platform}', 'tools/common', 'licenses', 'source/easy_flash'):
            (package / folder).mkdir(parents=True)
        launcher = 'Flash-Mac.command' if platform == 'macos' else 'Flash-Windows.bat'
        data = (TEMPLATES / launcher).read_text()
        if platform == 'windows':
            data = data.replace('\n', '\r\n')
        (package / launcher).write_bytes(data.encode('utf-8'))
        if platform == 'macos':
            (package / launcher).chmod(0o755)
        scripts = ('flash-mac.sh', 'flash-common.sh') if platform == 'macos' else ('flash-windows.ps1', 'flash-common.ps1')
        for script in scripts:
            encoding = 'utf-8-sig' if script.endswith('.ps1') else 'utf-8'
            (package / 'app' / script).write_bytes((TEMPLATES / script).read_text().encode(encoding))
        usage = (TEMPLATES / 'USAGE.txt').read_text().format(
            version=VERSION, platform='macOS' if platform == 'macos' else 'Windows',
            launcher=launcher, platform_notes=(TEMPLATES / f'USAGE-{platform}.txt').read_text().strip())
        (package / '使用说明.txt').write_bytes(usage.encode('utf-8-sig'))
        for asset in ('ds5dongle-lctech616-hs.bin', 'boot2_bl616_isp_release_v8.1.8.bin', 'partition.bin'):
            shutil.copyfile(firmware / asset, package / 'firmware' / asset)
        (package / 'firmware/version.txt').write_text(VERSION + '\n', encoding='utf-8')
        (package / 'firmware/flash_prog_cfg.ini').write_text(
            '[cfg]\nerase = 1\nskip_mode = 0x0, 0x0\nboot2_isp_mode = 0\n\n'
            '[boot2]\nfiledir = ./boot2_bl616_isp_release_v8.1.8.bin\naddress = 0x000000\n\n'
            '[partition]\nfiledir = ./partition.bin\naddress = 0xE000\n\n'
            '[FW]\nfiledir = ./ds5dongle-lctech616-hs.bin\naddress = @partition\n', encoding='ascii')

        executable = 'BLFlashCommand-macos' if platform == 'macos' else 'BLFlashCommand.exe'
        original = git(sdk, 'show', f'{SDK_COMMIT}:{cube}/{executable}')
        if platform == 'windows' and args.windows_exe and args.windows_exe.read_bytes() != original:
            raise SystemExit('Windows executable differs from pinned SDK version.')
        target = package / 'tools' / platform / executable
        target.write_bytes(original)
        target.chmod(0o755)
        if platform == 'macos':
            # ZIP extraction must not depend on symlink support.
            shutil.copytree(native / 'runtime', package / 'tools/macos-arm64', symlinks=False)
            shutil.copyfile(native / 'native-info.json', package / 'tools/macos-arm64/native-info.json')
            shutil.copytree(native / 'licenses', package / 'licenses/macOS-native')
            shutil.copyfile(native / 'vendor-source.tar.gz', package / 'source/native-flash-command-source.tar.gz')
            shutil.copyfile(native / 'build-requirements.txt', package / 'source/native-build-requirements.txt')
        assets = git(sdk, 'ls-tree', '-r', '--name-only', SDK_COMMIT,
                     f'{cube}/chips/bl616', f'{cube}/utils/pem').decode().splitlines()
        for asset in assets:
            relative = Path(asset).relative_to(cube)
            target = package / 'tools/common' / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(git(sdk, 'show', f'{SDK_COMMIT}:{asset}'))
        (package / 'licenses/FlashCube-ReleaseNote.txt').write_bytes(git(sdk, 'show', f'{SDK_COMMIT}:{cube}/ReleaseNote_En.txt'))
        (package / 'licenses/SDK-Apache-2.0.txt').write_bytes(git(sdk, 'show', f'{SDK_COMMIT}:LICENSE'))
        shutil.copyfile(REPO / 'LICENSE', package / 'licenses/Firmware-GPL-3.0.txt')
        shutil.copyfile(REPO / 'lib/opus/COPYING', package / 'licenses/Opus-COPYING.txt')
        signing = None
        if platform == 'macos':
            for arch, minimum in (('arm64', '11.0'), ('x86_64', '10.15')):
                subprocess.run(['xcrun', 'clang', '-arch', arch, f'-mmacosx-version-min={minimum}',
                                '-Os', '-Wall', '-Wextra', '-Werror', str(TEMPLATES / 'list_ports_macos.c'),
                                '-framework', 'IOKit', '-framework', 'CoreFoundation',
                                '-o', str(Path(temporary) / arch)], check=True)
            helper = package / 'app/list-ports-macos'
            subprocess.run(['xcrun', 'lipo', '-create', str(Path(temporary) / 'arm64'), str(Path(temporary) / 'x86_64'), '-output', str(helper)], check=True)
            subprocess.run(['codesign', '--force', '--sign', '-', str(helper)], check=True, capture_output=True)
            helper.chmod(0o755)
            signing = sign_macos_tools(package, args.sign_identity) if args.sign_identity else None
            if signing:
                native_info.setdefault('unsigned_executable_sha256', native_info['executable_sha256'])
                native_info['executable_sha256'] = sha(package / 'tools/macos-arm64/BLFlashCommand-macos')
                native_info['code_signing_identity'] = args.sign_identity
                (package / 'tools/macos-arm64/native-info.json').write_text(json.dumps(native_info, indent=2) + '\n')
        # Include all wrapper sources for reproducible builds; runtime files are platform-specific.
        for source in TEMPLATES.iterdir():
            if source.is_file():
                shutil.copyfile(source, package / 'source/easy_flash' / source.name)
        subprocess.run(['git', '-C', str(REPO), 'archive', '--format=tar.gz', '-o',
                        str(package / 'source/firmware-source.tar.gz'), 'HEAD'], check=True)
        metadata = {'version': VERSION, 'board': 'LCTech BL616', 'usb': 'HS', 'platform': platform,
                    'firmware_sha256': FIRMWARE_SHA, 'firmware_commit': revision,
                    'sdk_commit': SDK_COMMIT, 'flashcube_version': '1.4.2',
                    'firmware_hardware_tested': FIRMWARE_HARDWARE_TESTED}
        if platform == 'macos':
            metadata.update(macos_arm64_backend=native_info, macos_code_signing=signing,
                            macos_arm64_hardware_tested=False, macos_intel_hardware_tested=False)
        else:
            metadata['windows_hardware_tested'] = False
        (package / 'package-info.json').write_text(json.dumps(metadata, indent=2) + '\n')
        notice = (
            f'Firmware: {VERSION}\nPlatform: {platform}\nSource revision: {revision}\nSHA-256: {FIRMWARE_SHA}\n\n'
            'Firmware source snapshot: source/firmware-source.tar.gz\n'
            'Repository: https://github.com/chunqian/ds5dongle-bl618-opensource\n'
            f'SDK / FlashCube source and binaries: https://github.com/sqlCRT/bouffalo_sdk/tree/{SDK_COMMIT}\n'
            'Build: SDK in ../bouffalo_sdk, install the RISC-V toolchain, then USB_SPEED=hs bash build_macos.sh build\n'
            'Wrapper sources (both platforms, for rebuilding): source/easy_flash (GPL-3.0-or-later)\n'
            'Upstream license texts and release notes: licenses/\n'
            'Stock utils/pem files are public SDK test material; this package does not enable firmware signing or efuse programming.\n'
            'R6 firmware passed hardware flashing and read-back verification; each packaged tool still needs platform-specific validation.\n')
        if platform == 'macos':
            notice += (
                'Intel Mac FlashCube uses the pinned SDK binary, with its code signature replaced in signed packages.\n'
                'Apple Silicon uses unmodified bflb-flash-command 1.0.0 source, frozen with an ARM64 Python runtime.\n'
                f"Native source: {native_info['source_url']}\nSource SHA-256: {native_info['source_sha256']}\n"
                'Native runtime metadata: tools/macos-arm64/native-info.json; notices: licenses/macOS-native/\n'
                'Signed packages: signing-info.json identifies each certificate-signed Mac executable/library.\n'
                'Shell launchers are text files, not signed application bundles. The bundled tools are not notarized.\n')
        else:
            notice += 'Windows FlashCube is copied unchanged from the SDK. No Mac runtime is included.\n'
        (package / 'NOTICE.txt').write_text(notice, encoding='utf-8')
        checked = [p for p in package.rglob('*') if p.is_file() and p.relative_to(package).parts[0] in ('app', 'firmware', 'tools')]
        (package / 'asset-hashes.txt').write_text(''.join(
            f'{sha(p)}  {p.relative_to(package).as_posix()}\n' for p in sorted(checked)), encoding='ascii')
        archive = output / (name + '.zip')
        with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as zipped:
            for path in sorted(package.rglob('*')):
                if path.is_file():
                    zipped.write(path, str(Path(name) / path.relative_to(package)))
        expanded = output / name
        if expanded.exists():
            shutil.rmtree(expanded)
        shutil.copytree(package, expanded)
        (output / (archive.name + '.sha256')).write_text(f'{sha(archive)}  {archive.name}\n')
        print(archive)
        print(f'ZIP SHA-256: {sha(archive)}')
        print(f'Size: {archive.stat().st_size / 1024 / 1024:.1f} MiB')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk", type=Path, default=REPO.parent / "bouffalo_sdk")
    parser.add_argument("--windows-exe", type=Path)
    parser.add_argument("--platform", choices=('all', 'windows', 'macos'), default='all',
                        help='all builds two separate ZIPs (default)')
    parser.add_argument("--output", type=Path, default=REPO / "build/easy_flash")
    parser.add_argument("--native-dir", type=Path, default=REPO / "build/native_macos")
    parser.add_argument("--firmware-dir", type=Path, default=REPO / "firmware/lctech616",
                        help="Directory containing the pinned firmware, Boot2 and partition binaries")
    parser.add_argument("--sign-identity", help="Keychain certificate identity or SHA-1; signs all shipped Mac Mach-O files")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    firmware = args.firmware_dir.resolve()
    if args.platform == 'windows' and args.sign_identity:
        parser.error('--sign-identity applies only to --platform macos or all')
    if sha(firmware / "ds5dongle-lctech616-hs.bin") != FIRMWARE_SHA:
        raise SystemExit("Expected the pinned R6 HS binary; update version/hash deliberately for a new release.")
    if VERSION.encode() not in (firmware / "ds5dongle-lctech616-hs.bin").read_bytes():
        raise SystemExit("Firmware version does not match package metadata.")
    subprocess.run(["git", "-C", str(REPO), "diff", "--exit-code", "HEAD", "--", "src", "CMakeLists.txt", "cmake"], check=True)
    revision = git(REPO, "rev-parse", "HEAD").decode().strip()
    for platform in (('windows', 'macos') if args.platform == 'all' else (args.platform,)):
        build_platform(args, platform, revision)


if __name__ == '__main__':
    main()
