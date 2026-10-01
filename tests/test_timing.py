import unittest
from datetime import datetime, timedelta, timezone

import test_validation


class TimingTests(unittest.TestCase):
    def classify(self, ts, ingestion='2026-10-01T00:00:00Z', zone='UTC', start='2026-09-01', end='2026-10-01'):
        from rvn_ledger.timing import classify_time, period_bounds
        helper = test_validation.ValidationTests()
        event = helper.validate(helper.event(ts=ts, ingested_at=ingestion))
        return classify_time(event, period_bounds(start, end, zone, 48))

    def test_period_boundaries_in_all_source_zones(self):
        boundaries = {
            'UTC': ('2026-09-01T00:00:00Z', '2026-10-01T00:00:00Z'),
            'Europe/Istanbul': ('2026-08-31T21:00:00Z', '2026-09-30T21:00:00Z'),
            'Asia/Tokyo': ('2026-08-31T15:00:00Z', '2026-09-30T15:00:00Z'),
            'America/New_York': ('2026-09-01T04:00:00Z', '2026-10-01T04:00:00Z'),
        }
        for zone, (start, end) in boundaries.items():
            for boundary, expectations in [(start, ['excluded_out_of_period', 'accepted', 'accepted']), (end, ['accepted', 'excluded_out_of_period', 'excluded_out_of_period'])]:
                instant = datetime.fromisoformat(boundary.replace('Z', '+00:00'))
                for delta, expected in zip([-1, 0, 1], expectations):
                    with self.subTest(zone=zone, boundary=boundary, delta=delta):
                        self.assertEqual(self.classify((instant + timedelta(microseconds=delta)).isoformat(), zone=zone), expected)

    def test_late_boundary_is_inclusive(self):
        for zone, cutoff in [('UTC', '2026-10-03T00:00:00Z'), ('Europe/Istanbul', '2026-10-02T21:00:00Z'), ('Asia/Tokyo', '2026-10-02T15:00:00Z'), ('America/New_York', '2026-10-03T04:00:00Z')]:
            instant = datetime.fromisoformat(cutoff.replace('Z', '+00:00'))
            for delta, expected in [(-1, 'accepted'), (0, 'accepted'), (1, 'excluded_late')]:
                with self.subTest(zone=zone, delta=delta):
                    self.assertEqual(self.classify('2026-09-15T12:00:00Z', (instant + timedelta(microseconds=delta)).isoformat(), zone), expected)

    def test_outside_period_precedes_late(self):
        self.assertEqual(self.classify('2026-10-01T00:00:00Z', '2026-10-10T00:00:00Z'), 'excluded_out_of_period')

    def test_equivalent_offsets_have_same_result(self):
        # Review finding 7: a dropped offset must change the outcome, so the two instants
        # straddle the UTC period start (02:59:59+03:00 is 23:59:59Z the day before).
        self.assertEqual(self.classify('2026-09-01T03:00:00+03:00'), 'accepted')
        self.assertEqual(self.classify('2026-09-01T02:59:59+03:00'), 'excluded_out_of_period')
        self.assertEqual(self.classify('2026-09-30T20:59:59-03:00'), 'accepted')
        self.assertEqual(self.classify('2026-09-30T21:00:00-03:00'), 'excluded_out_of_period')

    def test_invalid_event_cannot_enter_time_filter(self):
        from rvn_ledger.timing import classify_time, period_bounds
        helper = test_validation.ValidationTests()
        with self.assertRaises(ValueError):
            classify_time(helper.validate(helper.event(units=0)), period_bounds('2026-09-01', '2026-10-01', 'UTC', 48))

    def test_dst_cutoff_is_elapsed_hours_not_wall_days(self):
        from rvn_ledger.timing import period_bounds
        bounds = period_bounds('2026-10-01', '2026-11-01', 'America/New_York', 48)
        self.assertEqual(bounds.end, datetime(2026, 11, 1, 4, tzinfo=timezone.utc))
        self.assertEqual(bounds.cutoff, datetime(2026, 11, 3, 4, tzinfo=timezone.utc))

    def test_bad_configuration_fails_closed(self):
        from rvn_ledger.timing import period_bounds
        from rvn_ledger.inputs import InputError
        # 'America' and 'Etc' are directories in the tzdata tree: ZoneInfo raises IsADirectoryError
        # (an OSError, not ZoneInfoNotFoundError) for them, which must still become InputError.
        for start, end, zone, hours in [('bad', '2026-10-01', 'UTC', 48), ('2026-10-01', '2026-09-01', 'UTC', 48), ('2026-09-01', '2026-10-01', 'bad/zone', 48), ('2026-09-01', '2026-10-01', 'UTC', True), ('2026-09-01', '2026-10-01', 'UTC', -1), ('2026-09-01', '2026-10-01', 'America', 48), ('2026-09-01', '2026-10-01', 'Etc', 48), ('2026-09-01', '2026-10-01', '', 48)]:
            with self.subTest(start=start, zone=zone, hours=hours), self.assertRaises(InputError):
                period_bounds(start, end, zone, hours)

    def test_period_dates_use_the_same_strict_parser_as_subscription(self):
        # Review F3: on CPython 3.11 date.fromisoformat also accepts '20260901' and '2026-W36-2',
        # so timing accepted periods that subscription refused. One shared strict parser
        # (inputs.local_date) now serves both: canonical YYYY-MM-DD only.
        from rvn_ledger import inputs
        from rvn_ledger import subscription
        from rvn_ledger import timing
        from rvn_ledger.inputs import InputError
        self.assertEqual(inputs.local_date('2026-09-01', 'x').isoformat(), '2026-09-01')
        for value in ('20260901', '2026-W36-2', '2026-09-01T00:00:00', '2026-9-1', ' 2026-09-01', 20260901, None, ''):
            with self.subTest(value=value):
                with self.assertRaises(InputError):
                    inputs.local_date(value, 'x')
                with self.assertRaises(InputError):
                    timing.period_bounds(value, '2026-10-01', 'UTC', 48)
                with self.assertRaises(InputError):
                    timing.period_bounds('2026-09-01', value, 'UTC', 48)
                with self.assertRaises(InputError):
                    subscription.period_days({'period_start_local': value, 'period_end_local_exclusive': '2026-10-01', 'days_in_period': 30})

    def test_host_dependent_zone_keys_are_rejected(self):
        # Reviewer hypothesis (round 3), confirmed on this host: ZoneInfo('localtime') and
        # ZoneInfo('Factory') resolve from the system tzdata tree, so an account carrying such a
        # key would be billed on server-dependent bounds. Only real IANA keys are usable.
        from rvn_ledger.timing import period_bounds
        from rvn_ledger.inputs import InputError
        for zone in ('localtime', 'posixrules', 'Factory', 'utc', 'europe/istanbul', 'Europe/Istanbul ', 'Etc/localtime'):
            with self.subTest(zone=zone), self.assertRaises(InputError):
                period_bounds('2026-09-01', '2026-10-01', zone, 48)
        for zone in ('UTC', 'Etc/UTC', 'Europe/Istanbul', 'America/New_York', 'Asia/Tokyo'):
            with self.subTest(zone=zone):
                period_bounds('2026-09-01', '2026-10-01', zone, 48)

    def test_account_bounds_resolves_every_account_eagerly(self):
        # Review finding 4: bounds for all accounts exist before any event is classified.
        from rvn_ledger.timing import account_bounds
        from rvn_ledger.inputs import InputError
        period = {'period_start_local': '2026-09-01', 'period_end_local_exclusive': '2026-10-01', 'late_cutoff_hours_after_period_end': 48}
        bounds = account_bounds([{'account_id': 'ist', 'timezone': 'Europe/Istanbul'}, {'account_id': 'utc', 'timezone': 'UTC'}], period)
        self.assertEqual(list(bounds), ['ist', 'utc'])
        self.assertEqual(bounds['ist'].start, datetime(2026, 8, 31, 21, tzinfo=timezone.utc))
        self.assertEqual(bounds['ist'].cutoff, datetime(2026, 10, 2, 21, tzinfo=timezone.utc))
        for accounts, bad_period in [([{'account_id': 'x', 'timezone': 'Mars/Olympus'}], period),
                                     ([{'account_id': 'x', 'timezone': 5}], period),
                                     ([{'account_id': 'x', 'timezone': 'UTC'}, {'account_id': 'x', 'timezone': 'UTC'}], period),
                                     ([{'account_id': 'x', 'timezone': 'UTC'}], {**period, 'late_cutoff_hours_after_period_end': '48'}),
                                     ([{'account_id': 'x', 'timezone': 'UTC'}], {k: v for k, v in period.items() if k != 'period_start_local'})]:
            with self.subTest(accounts=accounts, period=bad_period), self.assertRaises(InputError):
                account_bounds(accounts, bad_period)

    def test_submicrosecond_cutoff_and_period_boundaries(self):
        classify = self.classify
        for offset, cutoff in [('Z', '2026-10-03T00:00:00'), ('+03:00', '2026-10-03T03:00:00')]:
            self.assertEqual(classify('2026-09-15T12:00:00Z', cutoff + '.0000000' + offset), 'accepted')
            for digits in ('0000001', '000000000000000000001'):
                self.assertEqual(classify('2026-09-15T12:00:00Z', cutoff + '.' + digits + offset), 'excluded_late')
        self.assertEqual(classify('2026-09-30T23:59:59.999999999Z'), 'accepted')
        self.assertEqual(classify('2026-10-01T00:00:00.000000001Z'), 'excluded_out_of_period')
        self.assertEqual(classify('2026-08-31T23:59:59.999999999Z'), 'excluded_out_of_period')
