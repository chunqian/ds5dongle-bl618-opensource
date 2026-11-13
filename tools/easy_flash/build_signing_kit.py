#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Bundle the pinned Mac-only ZIP and a portable Mac signing script."""
import argparse
import json
from pathlib import Path
import shutil
import tempfile
import zipfile

from build_package import FIRMWARE_SHA, RELEASE, VERSION, package_name
from sign_macos_release import extract_package, sha, verify_package

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
KIT = f'DS5Dongle-{RELEASE}-Mac-Signing-Kit'
# Both the ARM64 and Intel backends produced this exact image in offline validation.
IMAGE_SHA = 'c7ef0d8988571c24677fd6b7be99725b163e1a5f90daf06669740b332927ca28'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', type=Path,
                        default=REPO / 'build/easy_flash' / (package_name('macos', True) + '.zip'))
    parser.add_argument('--output', type=Path, default=REPO / 'build/easy_flash')
    args = parser.parse_args()
    archive = args.package.resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='ds5-sign-kit-') as temporary:
        base = Path(temporary)
        package = extract_package(archive, base / 'verify')
        verify_package(package, FIRMWARE_SHA)
        if json.loads((package / 'package-info.json').read_text()).get('platform') != 'macos':
            raise SystemExit('Signing kit requires a Mac-only package')
        if (package / 'firmware/version.txt').read_text().strip() != VERSION:
            raise SystemExit(f'{RELEASE} version mismatch')
        kit = base / KIT
        (kit / 'payload').mkdir(parents=True)
        shutil.copyfile(archive, kit / 'payload' / archive.name)
        for name in ('Sign-Mac.command', 'sign_macos_release.py'):
            shutil.copyfile(HERE / name, kit / name)
            (kit / name).chmod(0o755)
        shutil.copyfile(HERE / 'SIGNING.txt', kit / '签名说明.txt')
        config = {'archive': archive.name, 'archive_sha256': sha(archive),
                  'version': VERSION, 'firmware_sha256': FIRMWARE_SHA,
                  'offline_image_sha256': IMAGE_SHA}
        (kit / 'signing-input.json').write_text(json.dumps(config, indent=2) + '\n')
        output = args.output / (KIT + '.zip')
        with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as zipped:
            for path in sorted(kit.rglob('*')):
                if path.is_file():
                    zipped.write(path, path.relative_to(base))
        (args.output / (KIT + '.zip.sha256')).write_text(f'{sha(output)}  {output.name}\n')
        print(output.resolve())
        print(f'SHA-256: {sha(output)}')
        print(f'Size: {output.stat().st_size / 1024 / 1024:.1f} MiB')


if __name__ == '__main__':
    main()
