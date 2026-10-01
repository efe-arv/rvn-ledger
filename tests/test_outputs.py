import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fixtures import ROOT, run_cli, run_fixture


class SerializationTests(unittest.TestCase):
    def test_bytes_are_stable_utf8_with_final_newline(self):
        from rvn_ledger.outputs import serialize
        document = [{'account_id': 'acct_ş', 'total_minor': 1, 'nested': {'b': None, 'a': [1, 2]}}]
        first, second = serialize(document), serialize(json.loads(json.dumps(document)))
        self.assertEqual(first, second)
        self.assertIsInstance(first, bytes)
        self.assertTrue(first.endswith(b'\n'))
        self.assertNotIn(b'\r', first)
        self.assertIn('acct_ş'.encode('utf-8'), first)           # UTF-8, not \u escapes
        self.assertEqual(json.loads(first.decode('utf-8')), document)
        self.assertEqual(list(json.loads(first.decode('utf-8'))[0]), ['account_id', 'total_minor', 'nested'])  # insertion order kept

    def test_non_finite_and_non_json_values_are_refused(self):
        from rvn_ledger.outputs import serialize
        for bad in ([float('nan')], [float('inf')], [{'a': {1, 2}}], [b'x'], [1.5]):
            with self.subTest(bad=bad), self.assertRaises((ValueError, TypeError)):
                serialize(bad)

    def test_serialization_preserves_invalid_identity_without_utf8_crash(self):
        from rvn_ledger.outputs import serialize
        document = {'id': '\ud800', 'normal': 'ş😀', '\udfff': ['\ud800']}
        encoded = serialize(document)
        self.assertEqual(json.loads(encoded.decode('utf-8')), document)
        self.assertIn('ş😀'.encode(), encoded)


class PublishTests(unittest.TestCase):
    def files(self):
        return {'invoices.json': b'[]\n', 'quarantine.json': b'[]\n', 'audit.json': b'{}\n'}

    def test_publish_writes_all_files_then_manifest_and_check_passes(self):
        from rvn_ledger.outputs import check_outputs, publish
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'out'
            manifest = publish(out, self.files(), {'counts': {'raw': 0}})
            self.assertEqual(sorted(p.name for p in out.iterdir()), ['audit.json', 'invoices.json', 'manifest.json', 'quarantine.json'])
            self.assertEqual(set(manifest['outputs']), {'invoices.json', 'quarantine.json', 'audit.json'})
            self.assertEqual(json.loads((out / 'manifest.json').read_text()), manifest)
            self.assertEqual(check_outputs(out)['outputs'], manifest['outputs'])

    def test_publish_twice_is_byte_identical_and_replaces_stale_outputs(self):
        from rvn_ledger.outputs import publish
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            (out / 'invoices.json').write_bytes(b'stale\n')
            publish(out, self.files(), {'counts': {'raw': 0}})
            first = {p.name: p.read_bytes() for p in out.iterdir()}
            publish(out, self.files(), {'counts': {'raw': 0}})
            second = {p.name: p.read_bytes() for p in out.iterdir()}
            self.assertEqual(first, second)
            self.assertEqual(first['invoices.json'], b'[]\n')

    def test_failed_publish_leaves_no_partial_output_set(self):
        # A write failure after some files were staged must not leave a directory that looks complete:
        # nothing is moved into place before every file (manifest included) exists in the staging area.
        from rvn_ledger.outputs import check_outputs, publish
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'out'
            publish(out, self.files(), {'counts': {'raw': 0}})
            before = {p.name: p.read_bytes() for p in out.iterdir()}
            with self.assertRaises(TypeError):
                publish(out, {'invoices.json': b'[1]\n', 'quarantine.json': 'not bytes', 'audit.json': b'{}\n'}, {'counts': {'raw': 1}})
            after = {p.name: p.read_bytes() for p in out.iterdir()}
            self.assertEqual(after, before)                       # previous complete set untouched
            self.assertEqual(check_outputs(out)['outputs'], json.loads(before['manifest.json'])['outputs'])
            self.assertFalse(any(p.name.startswith('.') for p in out.iterdir()))   # no staging leftovers

    def test_failure_while_moving_files_into_place_is_detectable(self):
        # A caught replacement failure restores the previous complete set.
        # Abrupt process/power failure is separately documented as fail-closed, not atomic.
        from rvn_ledger.outputs import check_outputs, publish
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            publish(out, self.files(), {'counts': {'raw': 0}})
            real_replace, calls = os.replace, []

            def flaky(src, dst):
                calls.append(dst)
                if len(calls) == 2:
                    raise OSError('disk gone')
                return real_replace(src, dst)
            with patch('rvn_ledger.outputs.os.replace', side_effect=flaky), self.assertRaises(OSError):
                publish(out, {'invoices.json': b'[1]\n', 'quarantine.json': b'[2]\n', 'audit.json': b'{"x": 1}\n'}, {'counts': {'raw': 1}})
            self.assertEqual(check_outputs(out)['counts'], {'raw': 0})
            self.assertFalse((out / '.ledger-staging').exists())

    def test_check_outputs_rejects_tampering_and_incomplete_sets(self):
        from rvn_ledger.outputs import OutputError, check_outputs, publish
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            publish(out, self.files(), {'counts': {'raw': 0}})
            (out / 'invoices.json').write_bytes(b'[{}]\n')
            with self.assertRaisesRegex(OutputError, 'invoices.json'):
                check_outputs(out)
            publish(out, self.files(), {'counts': {'raw': 0}})
            (out / 'quarantine.json').unlink()
            with self.assertRaisesRegex(OutputError, 'quarantine.json'):
                check_outputs(out)
            publish(out, self.files(), {'counts': {'raw': 0}})
            (out / 'manifest.json').unlink()
            with self.assertRaisesRegex(OutputError, 'manifest.json'):
                check_outputs(out)
            (out / 'manifest.json').write_text('{not json')
            with self.assertRaises(OutputError):
                check_outputs(out)

    def test_manifest_contains_no_clock_random_id_or_absolute_path(self):
        from rvn_ledger.outputs import publish
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / 'deep' / 'out'
            manifest = publish(out, self.files(), {'inputs': {'events.jsonl': {'sha256': 'x'}}, 'counts': {'raw': 0}})
            text = json.dumps(manifest)
            self.assertNotIn(tmp, text)
            self.assertNotIn(os.sep + 'home', text)
            for forbidden in ('timestamp', 'generated_at', 'run_id', 'uuid', 'created'):
                self.assertNotIn(forbidden, text.lower())
            self.assertNotIn('manifest.json', manifest['outputs'])   # no self-referential hash loop

    def test_malformed_output_metadata_is_controlled_error(self):
        from rvn_ledger.outputs import OutputError, check_outputs, publish
        for bad in (None, [], 'bad', 3, {}, {'sha256': 'x', 'bytes': True}):
            with self.subTest(bad=bad), tempfile.TemporaryDirectory() as tmp:
                publish(tmp, {'invoices.json': b'[]', 'quarantine.json': b'[]', 'audit.json': b'{}'}, {})
                path = Path(tmp) / 'manifest.json'
                manifest = json.loads(path.read_bytes())
                manifest['outputs']['invoices.json'] = bad
                path.write_text(json.dumps(manifest))
                with self.assertRaises(OutputError):
                    check_outputs(tmp)
                result = run_cli('check', '--out', tmp)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertNotIn('Traceback', result.stderr)

    def test_every_replacement_failure_restores_previous_good_output(self):
        from rvn_ledger.outputs import check_outputs, publish
        files = {'invoices.json': b'[]', 'quarantine.json': b'[]', 'audit.json': b'{}'}
        for fail_at in range(1, 5):
            with self.subTest(fail_at=fail_at), tempfile.TemporaryDirectory() as tmp:
                publish(tmp, files, {'counts': {'raw': 0}})
                before = {p.name: p.read_bytes() for p in Path(tmp).iterdir()}
                real_replace = os.replace
                calls = 0
                def fail_once(src, dst):
                    nonlocal calls
                    calls += 1
                    if calls == fail_at:
                        raise OSError('injected replace failure')
                    return real_replace(src, dst)
                with patch('rvn_ledger.outputs.os.replace', side_effect=fail_once), self.assertRaises(OSError):
                    publish(tmp, {n: b'[1]' for n in files}, {'counts': {'raw': 1}})
                self.assertEqual({p.name: p.read_bytes() for p in Path(tmp).iterdir()}, before)
                check_outputs(tmp)

    def test_actual_manifest_has_no_system_path(self):
        run = run_fixture()
        self.assertNotIn('/usr/', json.dumps(run.manifest))
        self.assertNotIn(str(ROOT), json.dumps(run.manifest))

    def test_staging_directory_blocks_publish_and_check_without_deletion(self):
        from rvn_ledger.outputs import OutputError, publish, check_outputs
        with tempfile.TemporaryDirectory() as tmp:
            staging = Path(tmp) / '.ledger-staging'
            staging.mkdir()
            (staging / 'recovery').write_bytes(b'keep')
            with self.assertRaises(OutputError):
                publish(tmp, {'invoices.json': b'[]', 'quarantine.json': b'[]', 'audit.json': b'{}'}, {})
            with self.assertRaises(OutputError):
                check_outputs(tmp)
            self.assertEqual((staging / 'recovery').read_bytes(), b'keep')

    def test_rollback_failure_keeps_recovery_and_fails_closed(self):
        from rvn_ledger.outputs import OutputError, publish, check_outputs
        with tempfile.TemporaryDirectory() as tmp:
            files = {'invoices.json': b'[]', 'quarantine.json': b'[]', 'audit.json': b'{}'}
            publish(tmp, files, {})
            actual = os.replace
            calls = 0
            def persistent_failure(src, dst):
                nonlocal calls
                calls += 1
                if calls >= 2:
                    raise OSError('persistent storage failure')
                return actual(src, dst)
            with patch('rvn_ledger.outputs.os.replace', side_effect=persistent_failure), self.assertRaises(OutputError):
                publish(tmp, {n: b'[1]' for n in files}, {})
            self.assertTrue((Path(tmp) / '.ledger-staging' / 'previous' / 'invoices.json').exists())
            with self.assertRaises(OutputError):
                check_outputs(tmp)
