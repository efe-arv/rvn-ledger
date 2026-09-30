import json
import os
import tempfile
import unittest
from pathlib import Path


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
        from unittest.mock import patch
        from rvn_ledger.outputs import OutputError, check_outputs, publish
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
