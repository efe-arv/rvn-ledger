import json
import unittest
from datetime import datetime, timezone

from rvn_ledger.inputs import read_events


def rows(values):
    return read_events(('\n'.join(json.dumps(v) for v in values) + '\n').encode())


class SelectionTests(unittest.TestCase):
    def select(self, values):
        from rvn_ledger.selection import deduplicate
        return deduplicate(rows(values))

    def test_smallest_sequence_wins_not_file_order(self):
        result = self.select([{'event_id': 'a', 'ingest_seq': 9},
                              {'event_id': 'a', 'ingest_seq': 2}])
        self.assertEqual([r.line_number for r in result.winners], [2])
        self.assertEqual(result.duplicates, {1: 2})

    def test_bad_first_payload_is_not_replaced(self):
        result = self.select([{'event_id': 'a', 'ingest_seq': 1, 'units': None},
                              {'event_id': 'a', 'ingest_seq': 2, 'units': 100}])
        self.assertIsNone(result.winners[0].value['units'])
        self.assertEqual(result.duplicates, {2: 1})

    def test_different_payload_still_duplicate(self):
        result = self.select([{'event_id': 'a', 'ingest_seq': 1, 'account_id': 'x'},
                              {'event_id': 'a', 'ingest_seq': 3, 'account_id': 'y'}])
        self.assertEqual(len(result.winners), 1)
        self.assertEqual(result.duplicates, {2: 1})

    def test_invalid_identity_or_sequence_is_quarantined(self):
        values = [{'ingest_seq': 1}, {'event_id': '', 'ingest_seq': 2},
                  {'event_id': 4, 'ingest_seq': 3}, {'event_id': 'a'},
                  {'event_id': 'b', 'ingest_seq': True},
                  {'event_id': 'c', 'ingest_seq': 1.0}]
        result = self.select(values)
        self.assertEqual(result.rejected, {1: 'invalid_event_id', 2: 'invalid_event_id',
                         3: 'invalid_event_id', 4: 'invalid_ingest_seq',
                         5: 'invalid_ingest_seq', 6: 'invalid_ingest_seq'})
        self.assertEqual(result.winners, [])

    def test_parse_errors_keep_original_reason(self):
        from rvn_ledger.selection import deduplicate
        result = deduplicate(read_events(b'bad\n[]\n\n'))
        self.assertEqual(result.rejected, {1: 'invalid_json', 2: 'not_object', 3: 'invalid_json'})

    def test_equal_sequence_uses_source_line_as_documented_tiebreak(self):
        result = self.select([{'event_id': 'a', 'ingest_seq': 1, 'units': 5},
                              {'event_id': 'a', 'ingest_seq': 1, 'units': 7}])
        self.assertEqual(result.winners[0].value['units'], 5)
        self.assertEqual(result.duplicates, {2: 1})

    def test_every_row_has_exactly_one_outcome(self):
        result = self.select([{'event_id': 'a', 'ingest_seq': 3},
                              {'event_id': 'b', 'ingest_seq': 1},
                              {'event_id': 'a', 'ingest_seq': 2}, {}])
        outcomes = [r.line_number for r in result.winners] + list(result.duplicates) + list(result.rejected)
        self.assertEqual(sorted(outcomes), [1, 2, 3, 4])

    def test_permutations_preserve_winner_payloads_when_sequences_unique(self):
        import itertools
        values = [{'event_id': 'a', 'ingest_seq': 7, 'units': 99},
                  {'event_id': 'a', 'ingest_seq': 2, 'units': 4},
                  {'event_id': 'b', 'ingest_seq': 3, 'units': 6}]
        for permutation in itertools.permutations(values):
            result = self.select(permutation)
            self.assertEqual({r.value['event_id']: r.value['units'] for r in result.winners},
                             {'a': 4, 'b': 6})

    # Review finding 1: a strictly rejected copy whose identity is readable still takes
    # part in ordering, so a later valid copy can never replace it (rule 2).
    TAIL = b',"account_id":"acct","metric":"api_calls","ts":"2026-09-10T00:00:00Z","ingested_at":"2026-09-10T00:01:00Z"}\n'

    def test_strictly_rejected_first_copy_is_not_replaced_by_later_valid_copy(self):
        from rvn_ledger.selection import deduplicate
        for broken in [b'{"event_id":"a","ingest_seq":1,"units":NaN',
                       b'{"event_id":"a","ingest_seq":1,"units":5,"units":7']:
            with self.subTest(broken=broken):
                result = deduplicate(read_events(broken + self.TAIL + b'{"event_id":"a","ingest_seq":2,"units":100' + self.TAIL))
                self.assertEqual(result.winners, [])
                self.assertEqual(result.rejected, {1: 'invalid_json'})
                self.assertEqual(result.duplicates, {2: 1})

    def test_strictly_rejected_later_copy_is_duplicate_not_quarantine(self):
        from rvn_ledger.selection import deduplicate
        result = deduplicate(read_events(b'{"event_id":"a","ingest_seq":1,"units":100' + self.TAIL + b'{"event_id":"a","ingest_seq":2,"units":NaN' + self.TAIL))
        self.assertEqual([r.line_number for r in result.winners], [1])
        self.assertEqual(result.duplicates, {2: 1})
        self.assertEqual(result.rejected, {})

    def test_ambiguous_identity_line_cannot_claim_an_event(self):
        # Documented fail-safe (architecture section 14, OPEN): repeated identity keys or a
        # truncated line keep no identity, so they cannot block or claim any event.
        from rvn_ledger.selection import deduplicate
        for broken in [b'{"event_id":"a","event_id":"a","ingest_seq":1,"units":NaN',
                       b'{"event_id":"a","ingest_seq":1,"units":']:
            with self.subTest(broken=broken):
                result = deduplicate(read_events(broken + self.TAIL + b'{"event_id":"a","ingest_seq":2,"units":100' + self.TAIL))
                self.assertEqual([r.line_number for r in result.winners], [2])
                self.assertEqual(result.rejected, {1: 'invalid_json'})
                self.assertEqual(result.duplicates, {})


class ClassificationTests(unittest.TestCase):
    # Review finding 4: the deduplicate -> validate -> time-filter glue is a product
    # function with one end-to-end test, not something each script rewrites.
    accounts = [{'account_id': 'acct', 'timezone': 'Europe/Istanbul'},
                {'account_id': 'idle', 'timezone': 'UTC'}]
    period = {'period_start_local': '2026-09-01', 'period_end_local_exclusive': '2026-10-01',
              'late_cutoff_hours_after_period_end': 48, 'metrics': ['api_calls', 'storage_gb_hours']}

    def event(self, **changes):
        value = dict(event_id='e', ingest_seq=1, account_id='acct', metric='api_calls', units=5,
                     ts='2026-09-10T00:00:00Z', ingested_at='2026-09-10T00:01:00Z')
        value.update(changes)
        return value

    def test_every_line_gets_exactly_one_terminal_status(self):
        from rvn_ledger.selection import classify_events, AcceptedEvent
        lines = [
            self.event(event_id='ok', ingest_seq=1),                                        # 1 accepted
            self.event(event_id='ok', ingest_seq=2, units=999),                             # 2 duplicate_ignored
            self.event(event_id='early', ingest_seq=3, ts='2026-08-31T20:59:59Z'),          # 3 Istanbul start is 08-31T21:00Z
            self.event(event_id='late', ingest_seq=4, ingested_at='2026-10-02T21:00:01Z'),  # 4 Istanbul end 09-30T21:00Z + 48h
            self.event(event_id='ghost', ingest_seq=5, account_id='nobody'),                # 5 quarantined
            self.event(event_id='zero', ingest_seq=6, units=0, metric='bad'),               # 6 quarantined, two reasons
        ]
        raw = '\n'.join(json.dumps(v) for v in lines).encode() + b'\n{broken\n'             # 7 quarantined, parse
        result = classify_events(read_events(raw), self.accounts, self.period)
        self.assertEqual(result.statuses, {1: 'accepted', 2: 'duplicate_ignored', 3: 'excluded_out_of_period',
                                           4: 'excluded_late', 5: 'quarantined', 6: 'quarantined', 7: 'quarantined'})
        self.assertEqual(result.reasons, {5: ('unknown_account',), 6: ('unknown_metric', 'nonpositive_units'), 7: ('invalid_json',)})
        self.assertEqual(result.duplicates, {2: 1})
        self.assertEqual([type(e) for e in result.accepted], [AcceptedEvent])
        self.assertEqual(result.accepted[0].row.line_number, 1)
        self.assertEqual(result.accepted[0].ts, datetime(2026, 9, 10, tzinfo=timezone.utc))
        self.assertEqual(result.counts, {'raw': 7, 'accepted': 1, 'duplicate_ignored': 1, 'excluded_out_of_period': 1,
                                         'excluded_late': 1, 'quarantined': 3})
        self.assertEqual(result.counts['raw'], sum(v for k, v in result.counts.items() if k != 'raw'))

    def test_strictly_rejected_first_copy_is_quarantined_end_to_end(self):
        from rvn_ledger.selection import classify_events
        raw = (b'{"event_id":"ok","ingest_seq":1,"units":NaN' + SelectionTests.TAIL
               + json.dumps(self.event(event_id='ok', ingest_seq=2, units=100)).encode() + b'\n')
        result = classify_events(read_events(raw), self.accounts, self.period)
        self.assertEqual(result.statuses, {1: 'quarantined', 2: 'duplicate_ignored'})
        self.assertEqual(result.reasons, {1: ('invalid_json',)})
        self.assertEqual(result.accepted, [])

    def test_nested_repeated_identity_key_does_not_let_later_copy_win(self):
        # Review N1 reproducer: the first copy is strictly rejected because a *nested*
        # object repeats an identity key; its own identity is clear, so the later valid
        # copy carrying 100 units must stay duplicate_ignored, never accepted.
        from rvn_ledger.selection import classify_events
        for nested in (b'"meta":{"event_id":"x","event_id":"y"}', b'"meta":{"ingest_seq":1,"ingest_seq":2}'):
            with self.subTest(nested=nested):
                raw = (b'{"event_id":"a","ingest_seq":1,"account_id":"acct","metric":"api_calls","units":5,'
                       b'"ts":"2026-09-10T00:00:00Z","ingested_at":"2026-09-10T00:01:00Z",' + nested + b'}\n'
                       + json.dumps(self.event(event_id='a', ingest_seq=2, units=100)).encode() + b'\n')
                result = classify_events(read_events(raw), self.accounts, self.period)
                self.assertEqual(result.statuses, {1: 'quarantined', 2: 'duplicate_ignored'})
                self.assertEqual(result.reasons, {1: ('invalid_json',)})
                self.assertEqual(result.accepted, [])

    def test_overlapping_deduplication_outcomes_are_refused(self):
        # Review N2: the "exactly one terminal status" guard must notice a line that
        # deduplicate reports twice, not just a line it forgot.
        from unittest.mock import patch
        from rvn_ledger.selection import Deduplication, classify_events
        rows = read_events(b'{"event_id":"a","ingest_seq":1}\n{"event_id":"b","ingest_seq":2}\n')
        overlapping = [Deduplication([], {1: 2}, {1: 'invalid_json', 2: 'x'}),   # review reproducer: rejected + duplicate
                       Deduplication([rows[0]], {2: 1}, {1: 'invalid_json'}),      # winner also rejected
                       Deduplication([rows[1]], {2: 1}, {1: 'x'})]                 # winner also duplicate
        for dedup in overlapping:
            with self.subTest(dedup=dedup), patch('rvn_ledger.selection.deduplicate', return_value=dedup):
                with self.assertRaisesRegex(RuntimeError, 'exactly one terminal status'):
                    classify_events(rows, self.accounts, self.period)
        with patch('rvn_ledger.selection.deduplicate', return_value=Deduplication([], {}, {1: 'x'})):
            with self.assertRaisesRegex(RuntimeError, 'exactly one terminal status'):
                classify_events(rows, self.accounts, self.period)                  # a forgotten line still fails

    def test_bad_configuration_fails_before_any_event_is_classified(self):
        from rvn_ledger.inputs import InputError
        from rvn_ledger.selection import classify_events
        without_cutoff = {k: v for k, v in self.period.items() if k != 'late_cutoff_hours_after_period_end'}
        bad = [
            ([{'account_id': 'idle', 'timezone': 'Mars/Olympus'}], self.period),
            ([{'account_id': 'a', 'timezone': 'UTC'}, {'account_id': 'a', 'timezone': 'UTC'}], self.period),
            ([{'account_id': 'a'}], self.period),
            ([{'account_id': '', 'timezone': 'UTC'}], self.period),
            (['acct'], self.period),
            ('acct', self.period),
            (self.accounts, {**self.period, 'metrics': ['api_calls', 'api_calls']}),
            (self.accounts, {**self.period, 'metrics': 'api_calls'}),
            (self.accounts, {**self.period, 'metrics': []}),
            (self.accounts, {**self.period, 'metrics': ['api_calls', 3]}),
            (self.accounts, without_cutoff),
            (self.accounts, []),
        ]
        for accounts, period in bad:
            with self.subTest(accounts=accounts, period=period), self.assertRaises(InputError):
                classify_events([], accounts, period)

    def test_empty_input_still_reconciles(self):
        from rvn_ledger.selection import classify_events
        result = classify_events([], self.accounts, self.period)
        self.assertEqual(result.counts, {'raw': 0})
        self.assertEqual(result.statuses, {})
        self.assertEqual(result.accepted, [])
