import hashlib
import unittest
from rvn_ledger.inputs import read_json, read_events, InputError


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


if __name__ == '__main__':
    unittest.main()
