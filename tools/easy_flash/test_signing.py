#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Failure-path tests for signing/notarization; no Apple uploads or real flashing."""
import contextlib
import io
import json
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import sign_macos_release as signing


class SigningTests(unittest.TestCase):
    def test_identity_selection_never_falls_back(self):
        with self.assertRaisesRegex(RuntimeError, '未找到'):
            signing.choose_identity([], None, False)
        available = [{'sha1': 'A' * 40, 'name': 'Developer ID Application: One (AAAAAAAAAA)'},
                     {'sha1': 'B' * 40, 'name': 'Developer ID Application: Two (BBBBBBBBBB)'}]
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, '多张'):
                signing.choose_identity(available, None, False)
        self.assertEqual(signing.choose_identity(available, 'b' * 40), available[1])
        output = ('  1) ' + 'A' * 40 + ' "Apple Development: Me (AAAAAAAAAA)"\n'
                  '  2) ' + 'B' * 40 + ' "Developer ID Application: Two (BBBBBBBBBB)"\n')
        with patch.object(signing, 'run', return_value=output):
            self.assertEqual(signing.identities(), [available[1]])

    def test_extract_rejects_paths_and_symlinks_before_writing(self):
        for invalid in ('root/../../escape', '/absolute', 'root/back\\slash'):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as temporary:
                base = Path(temporary)
                with zipfile.ZipFile(base / 'test.zip', 'w') as zipped:
                    zipped.writestr('root/good', 'good')
                    zipped.writestr(invalid, 'bad')
                with self.assertRaises(RuntimeError):
                    signing.extract_package(base / 'test.zip', base / 'out')
                self.assertFalse((base / 'out').exists())
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            info = zipfile.ZipInfo('root/link')
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            with zipfile.ZipFile(base / 'test.zip', 'w') as zipped:
                zipped.writestr(info, '/tmp/elsewhere')
            with self.assertRaises(RuntimeError):
                signing.extract_package(base / 'test.zip', base / 'out')

    def test_manifest_detects_extra_or_modified_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            (base / 'firmware').mkdir()
            firmware = base / 'firmware/ds5dongle-lctech616-hs.bin'
            firmware.write_bytes(b'firmware')
            digest = signing.sha(firmware)
            (base / 'asset-hashes.txt').write_text(f'{digest}  firmware/{firmware.name}\n')
            signing.verify_package(base, digest)
            (base / 'firmware/extra').write_bytes(b'extra')
            with self.assertRaisesRegex(RuntimeError, '未列入'):
                signing.verify_package(base, digest)
            (base / 'firmware/extra').unlink()
            firmware.write_bytes(b'changed')
            with self.assertRaises(RuntimeError):
                signing.verify_package(base, digest)

    def exercise_notary(self, status, *, resumed=False, timeout=False, tamper=False):
        with tempfile.TemporaryDirectory(prefix='ds5-notary-test-') as temporary:
            base = Path(temporary)
            dmg = base / 'test.dmg'
            dmg.write_bytes(b'test disk image')
            state = {'dmg': dmg.name, 'dmg_sha256': signing.sha(dmg), 'status': 'signed_not_notarized'}
            if resumed:
                state['submission_id'] = 'known-submission'
            if tamper:
                dmg.write_bytes(b'changed disk image')
            calls = []

            def fake_run(command, **kwargs):
                calls.append([str(value) for value in command])
                if 'submit' in command:
                    return json.dumps({'id': 'new-submission', 'message': 'Successfully uploaded file.'})
                if 'wait' in command:
                    if timeout:
                        raise subprocess.TimeoutExpired(command, 1860)
                    return json.dumps({'status': status})
                if 'staple' in command:
                    dmg.write_bytes(b'test disk image with ticket')
                return ''

            with patch.object(signing, 'run', side_effect=fake_run), contextlib.redirect_stdout(io.StringIO()):
                if tamper or timeout or status != 'Accepted':
                    with self.assertRaises((RuntimeError, subprocess.TimeoutExpired)):
                        signing.finish_notarization(base, state, 'test-profile')
                    self.assertNotEqual(state['status'], 'notarized')
                    self.assertFalse(any('staple' in call for call in calls))
                    if timeout:
                        saved = json.loads((base / 'release-result.json').read_text())
                        self.assertEqual(saved['submission_id'], 'new-submission')
                    if tamper:
                        self.assertEqual(calls, [])
                else:
                    signing.finish_notarization(base, state, 'test-profile')
                    self.assertEqual(state['status'], 'notarized')
                    self.assertEqual(state['dmg_sha256'], signing.sha(dmg))
                    self.assertIn(signing.sha(dmg), (base / 'test.dmg.sha256').read_text())
                    self.assertEqual(any('submit' in call for call in calls), not resumed)
                    self.assertTrue(any('/usr/sbin/spctl' in call for call in calls))

    def test_notary_acceptance_and_resume(self):
        self.exercise_notary('Accepted')
        self.exercise_notary('Accepted', resumed=True)

    def test_notary_invalid_timeout_and_tampering(self):
        self.exercise_notary('Invalid')
        self.exercise_notary('In Progress', timeout=True)
        self.exercise_notary('Accepted', resumed=True, tamper=True)

    def test_staple_failure_cannot_report_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            dmg = base / 'test.dmg'
            dmg.write_bytes(b'unchanged')
            state = {'dmg': dmg.name, 'dmg_sha256': signing.sha(dmg), 'submission_id': 'known'}

            def fail_staple(command, **kwargs):
                if 'staple' in command:
                    raise RuntimeError('stapler failed')
                return json.dumps({'status': 'Accepted'})

            with patch.object(signing, 'run', side_effect=fail_staple), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaisesRegex(RuntimeError, 'stapler failed'):
                    signing.finish_notarization(base, state, 'test')
            self.assertEqual(state['status'], 'accepted_not_stapled')


if __name__ == '__main__':
    unittest.main()
