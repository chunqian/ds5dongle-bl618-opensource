"""Offline signing tests. Uses ad-hoc signatures, never a private key or device."""
from pathlib import Path
import argparse
import json
import subprocess
import tempfile
from unittest.mock import patch

import sign_macos as signing


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package', type=Path, required=True)
    args = parser.parse_args()
    real_run = signing.run
    calls = []

    def adhoc(*command):
        calls.append(tuple(str(x) for x in command))
        if command[:2] == ('codesign', '--force'):
            command = list(command)
            command[command.index('--sign') + 1] = '-'
            command.remove('--timestamp')
            # Ad-hoc signatures have no Team ID; production retains runtime.
            # Offline tests verify ordering/seals and library loading separately.
            command.remove('--options')
            command.remove('runtime')
        return real_run(*command)

    def fail_expected(action, message):
        try:
            action()
        except (ValueError, subprocess.CalledProcessError):
            print('PASS:', message)
        else:
            raise AssertionError(message)

    with patch.object(signing, 'run', return_value=subprocess.CompletedProcess([], 0,
                      ' 1) ' + 'A' * 40 + ' "Apple Development: Test (TEAM)"\n', '')):
        fail_expected(lambda: signing.developer_id('A' * 40), 'reject Apple Development identity')
    with tempfile.TemporaryDirectory(prefix='ds5-sign-test-中文 ') as directory:
        work = Path(directory)
        app = work / 'DS5Dongle Flash.app'
        payload, metadata = signing.prepare_app(args.package.resolve(), app)
        with patch.object(signing, 'run', side_effect=adhoc):
            signing.sign_app(app, payload, metadata, 'A' * 40, 'TEST AD-HOC ONLY')
        assert any(call[:3] == ('codesign', '--verify', '--deep') for call in calls)
        signatures = [call for call in calls if call[:2] == ('codesign', '--force')]
        assert len(signatures) == 14 and signatures[-1][-1] == str(app)
        assert all('--timestamp' in call and '--options' in call for call in signatures)
        assert all('--deep' not in call for call in signatures)
        signing.verify_package(payload)
        print('PASS: inside-out signing of 13 payload binaries and outer app; rebuilt asset hashes and strict verification')
        for subdir, expected in [('macos-arm64', 'arm64'), ('macos', 'x86_64')]:
            info = json.loads(real_run(payload / 'tools' / subdir / 'BLFlashCommand-macos', '--runtime-info').stdout)
            assert info['machine'] == expected
        print('PASS: both signed runtimes load Python and extensions (Intel via Rosetta on Apple Silicon)')
        before = signing.runtime_assets(payload)
        real_run('/bin/bash', app / 'Contents/Resources/Run.command', '--check')
        assert before == signing.runtime_assets(payload)
        real_run('codesign', '--verify', '--deep', '--strict', app)
        print('PASS: app environment check preserves signed payload')
        upload = work / 'unsigned-test-upload.zip'
        signing.archive_app(app, upload)
        assert upload.is_file()
        for status in ('Invalid', 'In Progress'):
            response = subprocess.CompletedProcess([], 0, json.dumps({'id': 'test-id', 'status': status}), '')
            with patch.object(signing, 'archive_app'), patch.object(signing.subprocess, 'run', return_value=response), \
                 patch.object(signing, 'run') as apple:
                fail_expected(lambda: signing.notarize(app, work, 'test-profile'),
                              'no stapling or release success when notarization is ' + status)
                apple.assert_not_called()
        response = subprocess.CompletedProcess([], 0, json.dumps({'id': 'test-id', 'status': 'Accepted'}), '')
        with patch.object(signing, 'archive_app'), patch.object(signing.subprocess, 'run', return_value=response), \
             patch.object(signing, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')) as apple:
            assert signing.notarize(app, work, 'test-profile')['status'] == 'Accepted'
            assert [call.args[:3] for call in apple.call_args_list] == [
                ('xcrun', 'stapler', 'staple'), ('xcrun', 'stapler', 'validate'),
                ('codesign', '--verify', '--deep'), ('spctl', '--assess', '--type')]
        print('PASS: accepted receipt requires stapling, ticket validation and Gatekeeper assessment')
        (payload / 'firmware/partition.bin').write_bytes(b'corrupt')
        fail_expected(lambda: signing.verify_package(payload), 'reject modified firmware')
        fail_expected(lambda: real_run('codesign', '--verify', '--deep', '--strict', app), 'app seal detects modified resources')
    print('PASS: offline tests only; no Developer ID signing, Apple upload or hardware flashing performed')


if __name__ == '__main__':
    main()
