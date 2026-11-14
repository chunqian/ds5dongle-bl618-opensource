"""Build separate offline Windows and macOS LCTech BL616 HS flashing bundles.

Only the maintainer needs Python; macOS packages also need Xcode and both native
runtimes. End users need neither. --platform defaults to both.
"""
from pathlib import Path
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import zipfile

REPO = Path(__file__).resolve().parents[2]
TEMPLATES = Path(__file__).resolve().parent
SDK_COMMIT = "b3532953322194233f80436701e7fb6bb4ad8275"
FIRMWARE_SHA = "0f0452accc29497ebdc7b7a24f73281a0fb678388bea161652280d3511a7524c"
VERSION = "LCT616-DS5 3.18H-HidAudio-R5"
NAME = "DS5Dongle-LCTech616-HS-HidAudio-R5-Flash"

PLATFORMS = {
    "windows": {
        "suffix": "Windows", "launcher": "Flash-Windows.bat",
        "scripts": ("flash-windows.ps1", "flash-common.ps1"),
        "architectures": ["x86_64"],
        "notes": (
            "- 支持 64 位 Windows 10 / 11，使用系统自带 Windows PowerShell。\n"
            "- 不需要 Python、SDK、编译工具链或管理员运行。\n"
            "- 初次连接 BOOT 下载模式时，等待系统安装 USB 串口驱动，再按回车。\n"
            "- 若没有端口，在设备管理器中检查 USB 串行设备（COMx）的驱动状态。\n"
            "- 启动器只为本次进程设置 ExecutionPolicy Bypass，不修改全局策略。\n"
            "- 脚本模拟测试不等于 Windows 驱动和 EXE 的实机烧录验收。"
        ),
    },
    "macos": {
        "suffix": "Mac", "launcher": "Flash-Mac.command",
        "scripts": ("flash-mac.sh", "flash-common.sh"),
        "architectures": ["arm64", "x86_64"],
        "notes": (
            "- 支持 Apple 芯片和 Intel Mac（macOS 11 或更新），自动选择原生程序。\n"
            "- 不需要 Rosetta、Python、Homebrew、Xcode 或 SDK。\n"
            "- 本包尚未完成 Developer ID 签名及 Apple 公证。\n"
            "- 下载后如被 macOS 阻止，请在系统设置 → 隐私与安全性中允许此工具，\n"
            "  再重新打开；不要关闭系统整体安全检查。"
        ),
    },
}


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args])


def source_snapshot(worktree):
    revision = git(REPO, 'rev-parse', 'HEAD').decode().strip()
    if not worktree:
        subprocess.run(['git', '-C', str(REPO), 'diff', '--exit-code', 'HEAD', '--',
                        'src', 'lib', 'CMakeLists.txt', 'proj.conf'], check=True)
        return revision, git(REPO, 'rev-parse', 'HEAD^{tree}').decode().strip()
    # Capture tracked files plus nonignored source/test additions using a private
    # index. This creates a tree object, never a commit or a change to the user's index.
    with tempfile.TemporaryDirectory(prefix='ds5-source-index-') as directory:
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(directory) / 'index'))
        def indexed(*args):
            return subprocess.check_output(['git', '-C', str(REPO), *args], env=env)
        indexed('read-tree', revision)
        indexed('add', '-u', '--', '.')
        indexed('add', '--', 'src', 'lib', 'cmake', 'tests', 'tools/easy_flash')
        tree = indexed('write-tree').decode().strip()
    return revision, tree


def build_platform(args, platform, native_info, revision, source_tree):
    sdk = args.sdk.resolve()
    output = args.output.resolve()
    firmware = args.firmware_dir.resolve()
    spec = PLATFORMS[platform]
    name = NAME + "-" + spec["suffix"]
    cube = "tools/bflb_tools/bouffalo_flash_cube"
    with tempfile.TemporaryDirectory(prefix="ds5-package-") as temporary:
        package = Path(temporary) / name
        for folder in ("app", "firmware", f"tools/{platform}", "tools/common", "licenses", "source/easy_flash"):
            (package / folder).mkdir(parents=True)
        launcher = spec["launcher"]
        data = (TEMPLATES / launcher).read_text()
        if platform == "windows": data = data.replace('\n', '\r\n')
        (package / launcher).write_bytes(data.encode('utf-8'))
        if platform == "macos": (package / launcher).chmod(0o755)
        for script in spec["scripts"]:
            data = (TEMPLATES / script).read_text()
            encoding = "utf-8-sig" if script.endswith(".ps1") else "utf-8"
            (package / "app" / script).write_bytes(data.encode(encoding))
        usage = (TEMPLATES / "USAGE.txt").read_text()
        for key, value in {"VERSION": VERSION, "PLATFORM": spec["suffix"],
                           "LAUNCHER": launcher, "PLATFORM_NOTES": spec["notes"]}.items():
            usage = usage.replace("@" + key + "@", value)
        (package / "使用说明.txt").write_bytes(usage.encode("utf-8-sig"))
        for asset in ("ds5dongle-lctech616-hs.bin", "boot2_bl616_isp_release_v8.1.8.bin", "partition.bin"):
            shutil.copyfile(firmware / asset, package / "firmware" / asset)
        (package / "firmware/version.txt").write_text(VERSION + "\n", encoding="utf-8")
        (package / "firmware/flash_prog_cfg.ini").write_text(
            "[cfg]\nerase = 1\nskip_mode = 0x0, 0x0\nboot2_isp_mode = 0\n\n"
            "[boot2]\nfiledir = ./boot2_bl616_isp_release_v8.1.8.bin\naddress = 0x000000\n\n"
            "[partition]\nfiledir = ./partition.bin\naddress = 0xE000\n\n"
            "[FW]\nfiledir = ./ds5dongle-lctech616-hs.bin\naddress = @partition\n", encoding="ascii")

        # Use a pinned SDK revision, not generated loader/whole-flash files from a prior run.
        if platform == "windows":
            original = git(sdk, "show", f"{SDK_COMMIT}:{cube}/BLFlashCommand.exe")
            if args.windows_exe and args.windows_exe.read_bytes() != original:
                raise SystemExit("Windows executable differs from pinned SDK version.")
            target = package / "tools/windows/BLFlashCommand.exe"
            target.write_bytes(original)
            target.chmod(0o755)
        else:
            for arch, native, subdir, license_dir, source_prefix in (
                ('arm64', args.native_dir, 'macos-arm64', 'macOS-native', 'native'),
                ('x86_64', args.intel_native_dir, 'macos', 'macOS-Intel-native', 'intel-native'),
            ):
                shutil.copytree(native / 'runtime', package / 'tools' / subdir,
                                symlinks=False, dirs_exist_ok=True)
                shutil.copyfile(native / 'native-info.json', package / 'tools' / subdir / 'native-info.json')
                shutil.copytree(native / 'licenses', package / 'licenses' / license_dir)
                shutil.copyfile(native / 'vendor-source.tar.gz', package / 'source' / (source_prefix + '-flash-command-source.tar.gz'))
                shutil.copyfile(native / 'build-requirements.txt', package / 'source' / (source_prefix + '-build-requirements.txt'))
        assets = git(sdk, "ls-tree", "-r", "--name-only", SDK_COMMIT,
                     f"{cube}/chips/bl616", f"{cube}/utils/pem").decode().splitlines()
        for asset in assets:
            relative = Path(asset).relative_to(cube)
            target = package / "tools/common" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(git(sdk, "show", f"{SDK_COMMIT}:{asset}"))
        (package / "licenses/FlashCube-ReleaseNote.txt").write_bytes(git(sdk, "show", f"{SDK_COMMIT}:{cube}/ReleaseNote_En.txt"))
        (package / "licenses/SDK-Apache-2.0.txt").write_bytes(git(sdk, "show", f"{SDK_COMMIT}:LICENSE"))
        shutil.copyfile(REPO / "LICENSE", package / "licenses/Firmware-GPL-3.0.txt")
        shutil.copyfile(REPO / "lib/opus/COPYING", package / "licenses/Opus-COPYING.txt")
        if platform == "macos":
            for arch, minimum in (("arm64", "11.0"), ("x86_64", "10.15")):
                subprocess.run(["xcrun", "clang", "-arch", arch, f"-mmacosx-version-min={minimum}",
                                "-Os", "-Wall", "-Wextra", "-Werror", str(TEMPLATES / "list_ports_macos.c"),
                                "-framework", "IOKit", "-framework", "CoreFoundation",
                                "-o", str(Path(temporary) / arch)], check=True)
            helper = package / "app/list-ports-macos"
            subprocess.run(["xcrun", "lipo", "-create", str(Path(temporary) / "arm64"), str(Path(temporary) / "x86_64"), "-output", str(helper)], check=True)
            subprocess.run(["codesign", "--force", "--sign", "-", str(helper)], check=True, capture_output=True)
            helper.chmod(0o755)
        for source in TEMPLATES.iterdir():
            if source.is_file(): shutil.copyfile(source, package / "source/easy_flash" / source.name)
        subprocess.run(["git", "-C", str(REPO), "archive", "--format=tar.gz", "-o",
                        str(package / "source/firmware-source.tar.gz"), source_tree], check=True)
        metadata = {"version": VERSION, "board": "LCTech BL616", "usb": "HS",
                    "platform": platform, "architectures": spec["architectures"],
                    "firmware_sha256": FIRMWARE_SHA, "firmware_commit": revision,
                    "firmware_source_tree": source_tree,
                    "firmware_source_dirty": source_tree != git(REPO, 'rev-parse', revision + '^{tree}').decode().strip(),
                    "sdk_commit": SDK_COMMIT,
                    "package_hardware_tested": False}
        if platform == "macos":
            metadata["macos_arm64_backend"] = native_info['arm64']
            metadata["macos_intel_backend"] = native_info['x86_64']
        else:
            metadata["flashcube_version"] = "1.4.2"
        (package / "package-info.json").write_text(json.dumps(metadata, indent=2) + "\n")
        (package / "NOTICE.txt").write_text(
            f"Firmware: {VERSION}\nBase revision: {revision}\nSource tree: {source_tree}\nSHA-256: {FIRMWARE_SHA}\n\n"
            "Firmware source snapshot: source/firmware-source.tar.gz\n"
            "Repository: https://github.com/chunqian/ds5dongle-bl618-opensource\n"
            f"SDK / FlashCube source and binaries: https://github.com/sqlCRT/bouffalo_sdk/tree/{SDK_COMMIT}\n"
            "Build: SDK in ../bouffalo_sdk, install the RISC-V toolchain, then bash build_macos.sh build\n"
            "Wrapper sources: source/easy_flash (GPL-3.0-or-later)\n"
            "Upstream license texts and release notes: licenses/\n"
            "Stock utils/pem files are public SDK test material; this package does not enable firmware signing or efuse programming.\n"
            f"Platform: {platform}.\n" + (
                "Both Mac architectures use unmodified bflb-flash-command 1.0.0 source and independent onedir Python runtimes.\n"
                f"Native source: {native_info['arm64']['source_url']}\nSource SHA-256: {native_info['arm64']['source_sha256']}\n"
                "Native runtime metadata: tools/macos-arm64/native-info.json and tools/macos/native-info.json.\n"
                "Notices: licenses/macOS-native/ and licenses/macOS-Intel-native/.\n"
                "This package has not completed Developer ID signing or Apple notarization.\n"
                if platform == "macos" else "SDK Windows FlashCube binary is copied unchanged; native flashing still requires hardware acceptance testing.\n"
            ), encoding="utf-8")
        checked = [p for p in package.rglob('*') if p.is_file() and p.relative_to(package).parts[0] in ('app', 'firmware', 'tools')]
        (package / "asset-hashes.txt").write_text(''.join(
            f"{sha(p)}  {p.relative_to(package).as_posix()}\n" for p in sorted(checked)), encoding="ascii")
        archive = output / (name + ".zip")
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zipped:
            for path in sorted(package.rglob('*')):
                if path.is_file(): zipped.write(path, str(Path(name) / path.relative_to(package)))
        expanded = output / name
        if expanded.exists(): shutil.rmtree(expanded)
        shutil.copytree(package, expanded)
        archive.with_suffix('.zip.sha256').write_text(f"{sha(archive)}  {archive.name}\n", encoding="ascii")
        print(archive)
        print(f"ZIP SHA-256: {sha(archive)}")
        print(f"Size: {archive.stat().st_size / 1024 / 1024:.1f} MiB")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", choices=("both", "windows", "macos"), default="both")
    parser.add_argument("--sdk", type=Path, default=REPO.parent / "bouffalo_sdk")
    parser.add_argument("--windows-exe", type=Path)
    parser.add_argument("--output", type=Path, default=REPO / "build/easy_flash")
    parser.add_argument("--native-dir", type=Path, default=REPO / "build/native_macos")
    parser.add_argument("--intel-native-dir", type=Path, default=REPO / "build/native_macos_intel")
    parser.add_argument("--firmware-dir", type=Path, default=REPO / "firmware/lctech616")
    parser.add_argument('--snapshot-worktree', action='store_true',
                        help='Include uncommitted source changes in an explicit source-tree snapshot; never commit them')
    args = parser.parse_args()
    platforms = ("windows", "macos") if args.platform == "both" else (args.platform,)
    firmware = args.firmware_dir / "ds5dongle-lctech616-hs.bin"
    if sha(firmware) != FIRMWARE_SHA or VERSION.encode() not in firmware.read_bytes():
        raise SystemExit("Expected the pinned R5 HS binary; update version/hash deliberately for a new release.")
    revision, source_tree = source_snapshot(args.snapshot_worktree)
    if VERSION.encode() not in git(REPO, "show", source_tree + ":src/usb_gamepad.c"):
        raise SystemExit("Source snapshot firmware version differs from the packaged binary.")
    native_info = {}
    if "macos" in platforms:
        for arch, native in (('arm64', args.native_dir), ('x86_64', args.intel_native_dir)):
            info = json.loads((native / "native-info.json").read_text())
            if info['machine'] != arch or info['minimum_macos'] != '11.0':
                raise SystemExit(f'{arch} runtime must match its architecture and support macOS 11.')
            if arch == 'arm64' and info['rosetta']:
                raise SystemExit('Apple Silicon runtime must run natively.')
            if not (native / 'runtime/_internal').is_dir():
                raise SystemExit(f'{arch} requires an onedir runtime with all dependencies.')
            if sha(native / 'runtime/BLFlashCommand-macos') != info['executable_sha256']:
                raise SystemExit(f'{arch} executable differs from its build metadata.')
            native_info[arch] = info
        for key in ('backend', 'source_sha256', 'python'):
            if native_info['arm64'][key] != native_info['x86_64'][key]:
                raise SystemExit(f'Mac runtimes must use the same {key}.')
    args.output.mkdir(parents=True, exist_ok=True)
    for platform in platforms:
        build_platform(args, platform, native_info, revision, source_tree)


if __name__ == '__main__':
    main()
