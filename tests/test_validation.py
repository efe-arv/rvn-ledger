import unittest
from datetime import datetime, timezone

from rvn_ledger.selection import deduplicate

from test_selection import rows


class ValidationTests(unittest.TestCase):
    def event(self, **changes):
        value = dict(event_id='a', ingest_seq=1, account_id='acct',
                     metric='api_calls', units=5, ts='2026-09-01T00:00:00Z',
                     ingested_at='2026-09-01T01:00:00Z')
        value.update(changes)
        return value

    def validate(self, value):
        from rvn_ledger.validation import validate_event
        return validate_event(rows([value])[0], {'acct'}, {'api_calls', 'storage_gb_hours'})

    def test_valid_event_keeps_source_and_parsed_utc_times(self):
        result = self.validate(self.event())
        self.assertEqual(result.reasons, ())
        self.assertEqual(result.row.line_number, 1)
        self.assertEqual(result.ts, datetime(2026, 9, 1, tzinfo=timezone.utc))

    def test_missing_or_noninteger_units(self):
        for units in [None, True, False, 1.0, '5', [], {}]:
            with self.subTest(units=units):
                self.assertEqual(self.validate(self.event(units=units)).reasons, ('invalid_units',))
        value = self.event()
        del value['units']
        self.assertEqual(self.validate(value).reasons, ('invalid_units',))

    def test_nonpositive_units(self):
        for units in [0, -1]:
            self.assertEqual(self.validate(self.event(units=units)).reasons, ('nonpositive_units',))

    def test_unknown_account_without_unhashable_crash(self):
        for account in ['unknown', None, [], {}]:
            self.assertEqual(self.validate(self.event(account_id=account)).reasons, ('unknown_account',))

    def test_unknown_metric_without_unhashable_crash(self):
        for metric in ['unknown', None, [], {}]:
            self.assertEqual(self.validate(self.event(metric=metric)).reasons, ('unknown_metric',))

    def test_invalid_usage_timestamp(self):
        for ts in [None, 42, '', 'bad', '2026-02-30T00:00:00Z',
                   '2026-09-01', '2026-09-01T00:00:00']:
            self.assertEqual(self.validate(self.event(ts=ts)).reasons, ('invalid_ts',))

    def test_invalid_ingestion_timestamp(self):
        for ts in [None, 'bad', '2026-09-01T00:00:00']:
            self.assertEqual(self.validate(self.event(ingested_at=ts)).reasons, ('invalid_ingested_at',))

    def test_equivalent_offset_normalizes_to_same_instant(self):
        a = self.validate(self.event())
        b = self.validate(self.event(ts='2026-09-01T03:00:00+03:00'))
        self.assertEqual(a.ts, b.ts)

    def test_all_reasons_have_fixed_precedence(self):
        result = self.validate(self.event(account_id='bad', metric='bad', units=0,
                                          ts='bad', ingested_at='bad'))
        self.assertEqual(result.reasons, ('unknown_account', 'unknown_metric',
                         'nonpositive_units', 'invalid_ts', 'invalid_ingested_at'))

    def test_deduplication_cannot_repair_invalid_first_copy(self):
        chosen = deduplicate(rows([self.event(units=None), self.event(ingest_seq=2)]))
        from rvn_ledger.validation import validate_event
        result = validate_event(chosen.winners[0], {'acct'}, {'api_calls'})
        self.assertEqual(result.reasons, ('invalid_units',))
        self.assertEqual(chosen.duplicates, {2: 1})

    def test_large_integer_is_preserved(self):
        result = self.validate(self.event(units=10**50))
        self.assertEqual(result.reasons, ())
        self.assertEqual(result.row.value['units'], 10**50)
