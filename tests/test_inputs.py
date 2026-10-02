import copy
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from rvn_ledger.inputs import InputError, MAX_INTEGER_DIGITS, read_events, read_json

from fixtures import event, EXPECTED_INVOICES, PLANS, RAW_EVENTS, run_cli, write_fixture


class InputTests(unittest.TestCase):
    def test_json_and_hash(self):
        raw = b'{"units": 12345678901234567890}'
        value, digest = read_json(raw, 'accounts.json')
        self.assertEqual(value['units'], 12345678901234567890)
        self.assertEqual(digest, hashlib.sha256(raw).hexdigest())

    def test_configuration_errors_are_fatal(self):
        for raw in (b'{', b'{"x": NaN}', b'{"x":1,"x":2}', b'\xff'):
            with self.subTest(raw=raw), self.assertRaises(InputError):
                read_json(raw, 'plans.json')

    def test_malformed_lines_survive_with_source(self):
        raw = b'{"event_id":"a"}\n{broken\n{"event_id":"b"}\n'
        rows = read_events(raw)
        self.assertEqual(len(rows), 3)
        self.assertEqual([r.line_number for r in rows], [1, 2, 3])
        self.assertEqual(rows[0].value['event_id'], 'a')
        self.assertEqual(rows[1].error, 'invalid_json')
        self.assertEqual(rows[2].value['event_id'], 'b')
        self.assertEqual(rows[1].sha256, hashlib.sha256(b'{broken\n').hexdigest())

    def test_non_objects_and_blank_are_not_silently_dropped(self):
        rows = read_events(b'[]\nnull\n\n{"units":true}\n')
        self.assertEqual([r.error for r in rows], ['not_object', 'not_object', 'invalid_json', None])
        self.assertIs(rows[3].value['units'], True)

    def test_invalid_encoding_duplicate_keys_and_nonfinite(self):
        rows = read_events(b'\xff\n{"x":1,"x":2}\n{"x":Infinity}\n')
        self.assertEqual([r.error for r in rows], ['invalid_utf8', 'invalid_json', 'invalid_json'])

    def test_final_line_and_empty_file(self):
        self.assertEqual(len(read_events(b'{}')), 1)
        self.assertEqual(read_events(b''), [])

    def test_deep_nesting_is_quarantined_not_crashed(self):
        # Review finding 2: CPython raises RecursionError (not ValueError) on deep nesting.
        raw = b'[' * 100000 + b'\n{"event_id":"b"}\n'
        rows = read_events(raw)
        self.assertEqual([r.error for r in rows], ['invalid_json', None])
        self.assertEqual(rows[1].value['event_id'], 'b')
        with self.assertRaises(InputError):
            read_json(b'[' * 100000, 'plans.json')

    def test_overflowing_float_is_rejected_like_infinity(self):
        # Review finding 9: 1e400 silently parses to inf unless parse_float refuses it.
        self.assertEqual(read_events(b'{"x":1e400}\n')[0].error, 'invalid_json')
        self.assertEqual(read_events(b'{"x":-1e400}\n')[0].error, 'invalid_json')
        self.assertEqual(read_events(b'{"x":1.5}\n')[0].value['x'], 1.5)
        with self.assertRaises(InputError):
            read_json(b'{"credit_minor":1e400}', 'accounts.json')

    def test_strictly_rejected_line_keeps_only_unambiguous_identity(self):
        # Review finding 1: strict rejection must not discard a readable identity, but
        # identity is salvaged only when unambiguous, and the payload is never kept.
        from rvn_ledger.inputs import Identity
        huge = b'9' * 5000
        cases = [
            (b'{"event_id":"a","ingest_seq":1,"units":NaN}', Identity('a', 1)),
            (b'{"event_id":"a","ingest_seq":1,"units":5,"units":7}', Identity('a', 1)),
            (b'{"event_id":"a","ingest_seq":1,"units":' + huge + b'}', Identity('a', 1)),
            (b'{"event_id":"a","ingest_seq":1,"units":1e400}', Identity('a', 1)),
            (b'{"event_id":"a","event_id":"b","ingest_seq":1,"units":NaN}', None),
            (b'{"event_id":"a","ingest_seq":1,"ingest_seq":2,"units":NaN}', None),
            (b'{"event_id":"a","ingest_seq":' + huge + b',"units":NaN}', None),
            (b'{"event_id":"a","ingest_seq":1,"units":', None),
            (b'{"event_id":"a","ingest_seq":true,"units":NaN}', None),
            (b'{"event_id":"","ingest_seq":1,"units":NaN}', None),
            (b'[' * 100000, None),
            (b'\xff', None),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw[:70]):
                row = read_events(raw + b'\n')[0]
                self.assertIsNotNone(row.error)
                self.assertIsNone(row.value)
                self.assertEqual(row.salvaged_identity, expected)
        parsed = read_events(b'{"event_id":"a","ingest_seq":1}\n')[0]
        self.assertIsNone(parsed.error)
        self.assertIsNone(parsed.salvaged_identity)

    def test_nested_repeated_identity_keys_do_not_block_top_level_salvage(self):
        # Review N1: only repetition of the identity keys in the top-level object is
        # ambiguous; a repeated event_id/ingest_seq inside a nested object says nothing
        # about the line's own identity and must not hand the event to a later copy.
        from rvn_ledger.inputs import Identity
        nested = [b'{"event_id":"a","ingest_seq":1,"units":NaN,"meta":{"event_id":"x","event_id":"y"}}',
                  b'{"event_id":"a","ingest_seq":1,"units":NaN,"meta":{"ingest_seq":1,"ingest_seq":2}}',
                  b'{"event_id":"a","ingest_seq":1,"units":NaN,"meta":[{"event_id":"x","event_id":"y"}]}',
                  b'{"meta":{"event_id":"x","event_id":"y"},"event_id":"a","ingest_seq":1,"units":NaN}',
                  b'{"event_id":"a","ingest_seq":1,"meta":{"deep":{"ingest_seq":1,"ingest_seq":1}}}']
        for raw in nested:
            with self.subTest(raw=raw):
                row = read_events(raw + b'\n')[0]
                self.assertEqual(row.error, 'invalid_json')
                self.assertEqual(row.salvaged_identity, Identity('a', 1))
        top_level = [b'{"event_id":"a","event_id":"a","ingest_seq":1,"meta":{"event_id":"a"}}',
                     b'{"event_id":"a","ingest_seq":1,"ingest_seq":1,"units":NaN,"meta":{"x":1}}']
        for raw in top_level:
            with self.subTest(raw=raw):
                row = read_events(raw + b'\n')[0]
                self.assertEqual(row.error, 'invalid_json')
                self.assertIsNone(row.salvaged_identity)

    def test_configuration_surrogates_fail_closed(self):
        from rvn_ledger.inputs import InputError, read_json
        with self.assertRaises(InputError):
            read_json(b'{"x":"\\ud800"}', 'config.json')


if __name__ == '__main__':
    unittest.main()


class IntegerBoundaryTests(unittest.TestCase):
    def test_admission_boundary_and_identity_recovery(self):
        for sign in ('', '-'):
            for digits in (MAX_INTEGER_DIGITS - 1, MAX_INTEGER_DIGITS, MAX_INTEGER_DIGITS + 1, 4300):
                raw = ('{"event_id":"oversized","ingest_seq":1,"units":' + sign + '9' * digits + '}').encode()
                row = read_events(raw)[0]
                with self.subTest(sign=sign, digits=digits):
                    if digits <= MAX_INTEGER_DIGITS:
                        self.assertIsNone(row.error)
                        self.assertEqual(row.value['units'], int(sign + '9' * digits))
                    else:
                        self.assertEqual(row.error, 'integer_too_large')
                        self.assertEqual(row.salvaged_identity.event_id, 'oversized')
                        with self.assertRaises(InputError):
                            read_json(raw, 'plans.json')

    def test_original_4300_digit_reproducer_keeps_billing_other_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lines = []
            for i in range(2):
                text = json.dumps(event(f'huge-{i}', i, 'acct_a', units=0))
                lines.append(text.replace('"units": 0', '"units": ' + '9' * 4300).encode())
            raw = RAW_EVENTS + b'\n'.join(lines) + b'\n'
            inputs = write_fixture(root / 'in', raw=raw)
            out = root / 'out'
            result = run_cli('run', '--input-dir', inputs, '--out', out, '--json')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads((out / 'invoices.json').read_bytes()), EXPECTED_INVOICES)
            quarantined = json.loads((out / 'quarantine.json').read_bytes())
            self.assertEqual(quarantined[-2:], [{'event_id': f'huge-{i}', 'reason': 'integer_too_large'} for i in range(2)])
            self.assertEqual(run_cli('check', '--out', out).returncode, 0)

    def test_rejected_canonical_integer_cannot_be_replaced(self):
        from rvn_ledger.selection import deduplicate
        prefix = b'{"event_id":"same","ingest_seq":1,"units":'
        rows = read_events(prefix + b'9' * (MAX_INTEGER_DIGITS + 1) + b'}\n'
                           + b'{"event_id":"same","ingest_seq":2,"units":1}\n')
        result = deduplicate(rows)
        self.assertEqual(result.rejected, {1: 'integer_too_large'})
        self.assertEqual(result.duplicates, {2: 1})

    def test_maximum_admitted_products_serialize_under_minimum_python_limit(self):
        largest = 10**MAX_INTEGER_DIGITS - 1
        plans = copy.deepcopy(PLANS)
        for plan in plans.values():
            for prices in plan['prices'].values():
                prices['subscription_fee_minor'] = largest
                for tiers in prices['metrics'].values():
                    for tier in tiers:
                        tier['unit_price_micros'] = largest
        values = [event(f'boundary-{i}', i, 'acct_a', units=largest) for i in range(3)]
        raw = ('\n'.join(json.dumps(v) for v in values) + '\n').encode()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inputs = write_fixture(root / 'in', raw=raw, plans=plans)
            out = root / 'out'
            env = {**os.environ, 'PYTHONINTMAXSTRDIGITS': '640'}
            result = run_cli('run', '--input-dir', inputs, '--out', out, env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(run_cli('check', '--out', out, env=env).returncode, 0)
            invoices = json.loads((out / 'invoices.json').read_bytes())
            self.assertEqual(invoices[0]['billable_units']['api_calls'], largest * 3)


class FirstLineBomTests(unittest.TestCase):
    """A UTF-8 byte order mark on the FIRST event line (an editor or export artefact) is stripped before
    parsing and identity salvage, and nowhere else: every hash still covers the exact source bytes, a BOM on
    a later line is ordinary malformed JSON, and configuration files keep rejecting a BOM outright."""
    BOM = b'\xef\xbb\xbf'

    def test_first_line_bom_is_stripped_for_parse_and_salvage_but_not_hashing(self):
        from rvn_ledger.inputs import Identity
        first = b'{"event_id":"a","ingest_seq":1}'
        second = b'{"event_id":"b","ingest_seq":2,"units":NaN}'
        rows = read_events(self.BOM + first + b'\n' + self.BOM + second + b'\n')
        self.assertIsNone(rows[0].error)
        self.assertEqual(rows[0].value, {'event_id': 'a', 'ingest_seq': 1})
        self.assertEqual(rows[0].sha256, hashlib.sha256(self.BOM + first + b'\n').hexdigest())   # hash of the exact bytes
        self.assertEqual(rows[1].error, 'invalid_json')                                           # only the first line
        self.assertIsNone(rows[1].salvaged_identity)
        salvaged = read_events(self.BOM + second + b'\n')[0]
        self.assertEqual(salvaged.error, 'invalid_json')
        self.assertEqual(salvaged.salvaged_identity, Identity('b', 2))
        self.assertEqual(salvaged.sha256, hashlib.sha256(self.BOM + second + b'\n').hexdigest())
        self.assertEqual(read_events(self.BOM + b'\n')[0].error, 'invalid_json')                   # a BOM alone is no event
        self.assertEqual(read_events(self.BOM + self.BOM + first + b'\n')[0].error, 'invalid_json')  # exactly one BOM
        self.assertEqual(read_events(self.BOM + first)[0].value['event_id'], 'a')                   # no trailing newline
        for name in ('accounts.json', 'plans.json', 'period.json'):
            with self.subTest(name=name), self.assertRaises(InputError):
                read_json(self.BOM + b'{}', name)

    def test_bom_on_the_demo_keeps_the_first_copy_of_the_conflicting_duplicate(self):
        # The demo's first line is the canonical copy of `api-first`; its later copy names another account with
        # 999 units. Treating the BOM line as unreadable would hand the event to that copy and change two invoices.
        from fixtures import ROOT
        demo = ROOT / 'examples' / 'demo'
        raw = self.BOM + (demo / 'events.jsonl').read_bytes()
        with tempfile.TemporaryDirectory() as tmp:
            inputs = Path(tmp) / 'in'
            inputs.mkdir()
            (inputs / 'events.jsonl').write_bytes(raw)
            for name in ('accounts.json', 'plans.json', 'period.json'):
                (inputs / name).write_bytes((demo / name).read_bytes())
            out = Path(tmp) / 'out'
            result = run_cli('run', '--input-dir', inputs, '--out', out)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads((out / 'invoices.json').read_bytes()), json.loads((demo / 'expected-invoices.json').read_bytes()))
            self.assertEqual(json.loads((out / 'quarantine.json').read_bytes()), json.loads((demo / 'expected-quarantine.json').read_bytes()))
            manifest = json.loads((out / 'manifest.json').read_bytes())
            self.assertEqual(manifest['inputs']['events.jsonl'], {'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw), 'records': 9})
            decisions = json.loads((out / 'audit.json').read_bytes())['decisions']
            self.assertEqual(decisions[0]['sha256'], hashlib.sha256(raw.split(b'\n')[0] + b'\n').hexdigest())
            self.assertEqual((decisions[0]['event_id'], decisions[0]['status']), ('api-first', 'accepted'))
            self.assertEqual((decisions[1]['status'], decisions[1]['canonical_line']), ('duplicate_ignored', 1))
            self.assertEqual(run_cli('check', '--out', out).returncode, 0)
            explained = run_cli('explain', '--out', out, '--event-id', 'api-first', '--events', inputs / 'events.jsonl', '--json')
            self.assertEqual(explained.returncode, 0, explained.stderr)
            self.assertTrue(json.loads(explained.stdout)['source_values_verified'])


class LeadingByteOrderMarkTests(unittest.TestCase):
    """A UTF-8 byte order mark at the very start of events.jsonl is an encoding artefact of the file,
    not of its first record: it is removed before the first line is parsed or its identity salvaged,
    while the line's recorded SHA-256 (and the file hash in the manifest) stay those of the raw bytes.
    A BOM anywhere else, or on configuration files, is still an error."""

    BOM = b'\xef\xbb\xbf'
    LINE = b'{"event_id":"a","ingest_seq":1}'

    def test_first_line_bom_is_stripped_for_parsing_but_hashed_as_written(self):
        rows = read_events(self.BOM + self.LINE + b'\n' + self.LINE + b'\n')
        self.assertIsNone(rows[0].error)
        self.assertEqual(rows[0].value, {'event_id': 'a', 'ingest_seq': 1})
        self.assertEqual(rows[0].sha256, hashlib.sha256(self.BOM + self.LINE + b'\n').hexdigest())
        self.assertEqual(rows[1].sha256, hashlib.sha256(self.LINE + b'\n').hexdigest())
        # The final line without a trailing newline is hashed as written too.
        self.assertEqual(read_events(self.BOM + self.LINE)[0].sha256, hashlib.sha256(self.BOM + self.LINE).hexdigest())

    def test_first_line_bom_does_not_block_identity_salvage(self):
        from rvn_ledger.inputs import Identity
        row = read_events(self.BOM + b'{"event_id":"a","ingest_seq":1,"units":NaN}\n')[0]
        self.assertEqual(row.error, 'invalid_json')
        self.assertIsNone(row.value)
        self.assertEqual(row.salvaged_identity, Identity('a', 1))

    def test_bom_elsewhere_is_still_rejected(self):
        later = read_events(self.LINE + b'\n' + self.BOM + self.LINE + b'\n')
        self.assertIsNone(later[0].error)
        self.assertEqual(later[1].error, 'invalid_json')
        self.assertIsNone(later[1].salvaged_identity)
        doubled = read_events(self.BOM + self.BOM + self.LINE + b'\n')[0]
        self.assertEqual(doubled.error, 'invalid_json')
        self.assertIsNone(doubled.salvaged_identity)
        self.assertEqual(read_events(self.BOM + b'\n')[0].error, 'invalid_json')   # a BOM alone is an empty first line
        self.assertEqual(read_events(self.BOM)[0].error, 'invalid_json')
        self.assertEqual(len(read_events(self.BOM)), 1)
        for name in ('accounts.json', 'plans.json', 'period.json'):
            with self.subTest(name=name), self.assertRaises(InputError):
                read_json(self.BOM + b'{}', name)
