"""Freeze the verified upstream Python source into a self-contained ARM64 binary.

Run with the ARM64 build venv, after installing native-requirements.txt:
  python tools/easy_flash/build_native_macos.py --archive /path/bflb-flash-command-1.0.0.tar.gz
"""
from pathlib import Path
import argparse
import hashlib
import importlib.metadata
import json
import platform
import re
import shutil
import subprocess
import sys
import sysconfig
import tarfile
import tempfile
import urllib.request

SOURCE_NAME = 'bflb-flash-command-1.0.0'
SOURCE_SHA = '5e637d4f55cecd40c5fca2bb870233bb2ae41b8e8f0f6ef883f16b4a3a11da72'
SOURCE_URL = ('https://files.pythonhosted.org/packages/8b/12/'
              '225cdf9fb96406dd8113cca49d47e2d70074e08b5292decf6ecc0be8d7f2/' + SOURCE_NAME + '.tar.gz')
REPO = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path)
    parser.add_argument('--output', type=Path, default=REPO / 'build/native_macos')
    parser.add_argument('--python-provenance', type=Path)
    parser.add_argument('--python-licenses', type=Path,
                        help='Directory of dependency notices from the matching full Python archive')
    args = parser.parse_args()
    if sys.platform != 'darwin' or platform.machine() != 'arm64':
        raise SystemExit('Build using a native ARM64 Python on an Apple Silicon Mac.')
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='ds5-arm64-build-') as d:
        stage = Path(d)
        archive = args.archive or stage / (SOURCE_NAME + '.tar.gz')
        if not args.archive:
            with urllib.request.urlopen(SOURCE_URL, timeout=60) as response, archive.open('wb') as out:
                shutil.copyfileobj(response, out)
        if hashlib.sha256(archive.read_bytes()).hexdigest() != SOURCE_SHA:
            raise SystemExit('Upstream source hash mismatch.')
        with tarfile.open(archive) as source:
            source.extractall(stage, filter='data')
        upstream = stage / SOURCE_NAME
        code = upstream / 'bflb_flash_command'
        # Leave upstream sources unmodified; ship only a CLI entry point of our own.
        subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--onedir',
                        '--target-arch', 'arm64', '--name', 'BLFlashCommand-macos',
                        '--distpath', str(stage / 'dist'), '--workpath', str(stage / 'work'),
                        '--specpath', str(stage), '--paths', str(code),
                        '--collect-submodules', 'libs', '--hidden-import', 'config',
                        '--exclude-module', 'PySide2', '--exclude-module', 'PySide6',
                        str(Path(__file__).with_name('native_macos_entry.py'))], check=True)
        bundle = stage / 'dist/BLFlashCommand-macos'
        executable = bundle / 'BLFlashCommand-macos'
        info = json.loads(subprocess.check_output([str(executable), '--runtime-info'], text=True))
        if info['machine'] != 'arm64' or info['rosetta']:
            raise SystemExit('The frozen program is not running natively.')
        # Reject any Mach-O dependency lacking an ARM64 slice, including Python itself.
        macho_count = 0
        minimum_os = (11, 0)
        for path in bundle.rglob('*'):
            if not path.is_file(): continue
            kind = subprocess.check_output(['file', '-b', str(path)], text=True)
            if 'Mach-O' in kind:
                archs = subprocess.check_output(['xcrun', 'lipo', '-archs', str(path)], text=True).split()
                if 'arm64' not in archs: raise SystemExit(f'Non-native dependency: {path}')
                builds = subprocess.check_output(['xcrun', 'vtool', '-show-build', str(path)], text=True)
                for version in re.findall(r'\bminos\s+([0-9.]+)', builds):
                    required = tuple(int(part) for part in version.split('.')[:2])
                    if required > minimum_os:
                        raise SystemExit(f'{path} requires macOS {version}; use a portable macOS 11+ Python and wheels.')
                links = subprocess.check_output(['otool', '-L', str(path)], text=True)
                if re.search(r'^\s+/(?:opt/homebrew|Users|private|tmp)/', links, re.M):
                    raise SystemExit(f'Unbundled development dependency: {path}\n{links}')
                macho_count += 1
        target = output / 'runtime'
        if target.exists(): shutil.rmtree(target)
        shutil.copytree(bundle, target, symlinks=True)
        license_dir = output / 'licenses'
        if license_dir.exists(): shutil.rmtree(license_dir)
        license_dir.mkdir()
        shutil.copyfile(upstream / 'LICENSE', license_dir / 'Bouffalo-Flash-Command-MIT.txt')
        # Preserve notices of build/runtime distributions, including Python.
        for dist in importlib.metadata.distributions():
            for entry in dist.files or []:
                if entry.name.lower().startswith(('license', 'copying', 'notice')) and entry.suffix not in ('.py', '.pyc'):
                    src = Path(dist.locate_file(entry))
                    if src.is_file():
                        dst = license_dir / dist.metadata['Name'] / str(entry)
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(src, dst)
        for root in (Path(sys.base_prefix), Path(sys.base_prefix).parent):
            candidates = list(root.glob('LICENSE*')) + list(root.glob('Resources/English.lproj/License*'))
            for candidate in candidates:
                if candidate.is_file(): shutil.copyfile(candidate, license_dir / ('Python-' + candidate.name))
        python_licenses = Path(sys.base_prefix) / 'share/licenses'
        if python_licenses.is_dir(): shutil.copytree(python_licenses, license_dir / 'Python-runtime', dirs_exist_ok=True)
        stdlib_license = Path(sysconfig.get_path('stdlib')) / 'LICENSE.txt'
        if stdlib_license.is_file(): shutil.copyfile(stdlib_license, license_dir / 'Python-LICENSE.txt')
        if args.python_licenses:
            shutil.copytree(args.python_licenses, license_dir / 'Python-dependencies', dirs_exist_ok=True)
        # MIT only requires its notice; include a useful, small source snapshot too.
        with tarfile.open(output / 'vendor-source.tar.gz', 'w:gz') as tar:
            tar.add(upstream / 'LICENSE', arcname=SOURCE_NAME + '/LICENSE')
            for path in sorted(code.rglob('*.py')):
                tar.add(path, arcname=str(Path(SOURCE_NAME) / path.relative_to(upstream)))
        freeze = subprocess.check_output([sys.executable, '-m', 'pip', 'freeze'], text=True)
        (output / 'build-requirements.txt').write_text(freeze)
        info.update(source_url=SOURCE_URL, source_sha256=SOURCE_SHA, macho_files_checked=macho_count,
                    minimum_macos='11.0',
                    executable_sha256=hashlib.sha256((target / 'BLFlashCommand-macos').read_bytes()).hexdigest())
        if args.python_provenance:
            info['python_distribution'] = json.loads(args.python_provenance.read_text())
        (output / 'native-info.json').write_text(json.dumps(info, indent=2) + '\n')
        print(json.dumps(info, indent=2))
        print('Native runtime:', target)


if __name__ == '__main__':
    main()
