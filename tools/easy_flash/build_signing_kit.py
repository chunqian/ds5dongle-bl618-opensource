"""Create a self-contained signing kit to take to a different Mac."""
from pathlib import Path
import argparse
import shutil
import tempfile
import zipfile

from sign_macos import sha, verify_package

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', type=Path, required=True)
    parser.add_argument('--output', type=Path, default=REPO / 'build/easy_flash')
    args = parser.parse_args()
    verify_package(args.package)
    args.output.mkdir(parents=True, exist_ok=True)
    name = args.package.name + '-Signing-Kit'
    with tempfile.TemporaryDirectory(prefix='ds5-signing-kit-') as temporary:
        kit = Path(temporary) / name
        kit.mkdir()
        shutil.copytree(args.package, kit / 'input/package')
        for filename in ('sign_macos.py', 'mac_launcher.m', 'Sign-Mac.command',
                         'Run-Mac-App.command', 'Flash-Mac.command', 'flash-mac.sh',
                         'flash-common.sh', 'SIGNING.txt'):
            shutil.copyfile(HERE / filename, kit / filename)
            if filename.endswith('.command'):
                (kit / filename).chmod(0o755)
        files = sorted(p for p in kit.rglob('*') if p.is_file())
        (kit / 'kit-hashes.txt').write_text(''.join(
            f'{sha(p)}  {p.relative_to(kit).as_posix()}\n' for p in files), encoding='utf-8')
        archive = args.output / (name + '.zip')
        # Refuse replacement so previously delivered kits remain auditable.
        if archive.exists() or (args.output / name).exists():
            raise SystemExit('Output already exists; choose another --output directory.')
        with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as zipped:
            for path in sorted(kit.rglob('*')):
                if path.is_file(): zipped.write(path, str(Path(name) / path.relative_to(kit)))
        archive.with_suffix('.zip.sha256').write_text(f'{sha(archive)}  {archive.name}\n')
        shutil.copytree(kit, args.output / name)
        print(archive.resolve())


if __name__ == '__main__':
    main()
