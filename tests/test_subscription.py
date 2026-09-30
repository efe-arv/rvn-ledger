import unittest
from datetime import date
from fractions import Fraction
from rvn_ledger.inputs import InputError

# Fees are chosen so that proration is observable: 2900 * 16 / 30 = 1546.67 -> 1547,
# and 15 * 1 / 30 = 0.5 -> 1 (round half up). Extra keys mimic the real plans.json shape.
PLANS = {
    'starter': {'plan_id': 'starter', 'name': 'Starter',
                'prices': {'USD': {'subscription_fee_minor': 2900, 'metrics': {}},
                           'EUR': {'subscription_fee_minor': 2700, 'metrics': {}}}},
    'scale': {'plan_id': 'scale', 'name': 'Scale',
              'prices': {'USD': {'subscription_fee_minor': 29900, 'metrics': {}},
                         'EUR': {'subscription_fee_minor': 27900, 'metrics': {}}}},
    'half': {'plan_id': 'half', 'prices': {'USD': {'subscription_fee_minor': 15}}},
    'usd_only': {'plan_id': 'usd_only', 'prices': {'USD': {'subscription_fee_minor': 100}}},
}
PERIOD = {'period_start_local': '2026-09-01', 'period_end_local_exclusive': '2026-10-01', 'days_in_period': 30,
          'late_cutoff_hours_after_period_end': 48, 'metrics': ['api_calls', 'storage_gb_hours']}


def segment(plan_id, start, end):
    return {'plan_id': plan_id, 'from': start, 'to': end}


def account(segments, currency='USD', account_id='acct_x', zone='UTC'):
    return {'account_id': account_id, 'name': 'x', 'timezone': zone, 'currency': currency,
            'credit_minor': 0, 'plan_segments': segments}


def reference_amount(fee, days, period_days):
    # Independent reference: exact rational arithmetic, half rounds up.
    return int(Fraction(fee * days, period_days) + Fraction(1, 2))


class SubscriptionTests(unittest.TestCase):
    def resolve(self, segments, currency='USD', period=PERIOD, plans=PLANS):
        from rvn_ledger.subscription import account_subscription
        return account_subscription(account(segments, currency), period, plans)

    def summary(self, sub):
        return [(s.plan_id, s.start.isoformat(), s.end.isoformat(), s.days, s.amount_minor) for s in sub.segments]

    def test_full_period_single_segment_is_full_fee(self):
        sub = self.resolve([segment('starter', '2026-09-01', '2026-10-01')])
        self.assertEqual(self.summary(sub), [('starter', '2026-09-01', '2026-10-01', 30, 2900)])
        self.assertEqual(sub.period_end_plan_id, 'starter')
        self.assertEqual(sub.currency, 'USD')
        self.assertEqual(sub.account_id, 'acct_x')

    def test_mid_period_plan_change_prorates_each_segment(self):
        sub = self.resolve([segment('starter', '2026-09-01', '2026-09-17'), segment('scale', '2026-09-17', '2026-10-01')])
        self.assertEqual(self.summary(sub), [('starter', '2026-09-01', '2026-09-17', 16, 1547),
                                             ('scale', '2026-09-17', '2026-10-01', 14, 13953)])
        self.assertEqual([s.amount_minor for s in sub.segments],
                         [reference_amount(2900, 16, 30), reference_amount(29900, 14, 30)])
        self.assertEqual(sub.period_end_plan_id, 'scale')

    def test_half_minor_unit_rounds_up_per_segment(self):
        sub = self.resolve([segment('half', '2026-09-01', '2026-09-02'), segment('starter', '2026-09-02', '2026-10-01')])
        self.assertEqual(sub.segments[0].days, 1)
        self.assertEqual(sub.segments[0].amount_minor, 1)
        self.assertEqual(sub.segments[0].amount_minor, reference_amount(15, 1, 30))

    def test_amounts_are_exact_integers_never_floats(self):
        sub = self.resolve([segment('starter', '2026-09-01', '2026-09-17'), segment('scale', '2026-09-17', '2026-10-01')])
        for seg in sub.segments:
            self.assertIs(type(seg.amount_minor), int)
            self.assertIs(type(seg.days), int)
            self.assertIs(type(seg.fee_minor), int)

    def test_segments_are_clipped_to_the_period(self):
        sub = self.resolve([segment('starter', '2026-08-15', '2026-09-10'), segment('scale', '2026-09-10', '2026-11-01')])
        self.assertEqual(self.summary(sub), [('starter', '2026-09-01', '2026-09-10', 9, reference_amount(2900, 9, 30)),
                                             ('scale', '2026-09-10', '2026-10-01', 21, reference_amount(29900, 21, 30))])
        self.assertEqual(sub.period_end_plan_id, 'scale')

    def test_segments_outside_the_period_are_dropped(self):
        # A segment ending exactly at period start and one starting exactly at period end
        # cover no local day of the period and produce no line.
        sub = self.resolve([segment('scale', '2026-07-01', '2026-09-01'), segment('starter', '2026-09-01', '2026-10-01'),
                            segment('scale', '2026-10-01', '2026-11-01')])
        self.assertEqual(self.summary(sub), [('starter', '2026-09-01', '2026-10-01', 30, 2900)])
        self.assertEqual(sub.period_end_plan_id, 'starter')

    def test_plan_change_on_last_local_day_selects_new_plan(self):
        sub = self.resolve([segment('starter', '2026-09-01', '2026-09-30'), segment('scale', '2026-09-30', '2026-10-01')])
        self.assertEqual([(s.plan_id, s.days) for s in sub.segments], [('starter', 29), ('scale', 1)])
        self.assertEqual(sub.period_end_plan_id, 'scale')

    def test_plan_change_at_exclusive_period_end_keeps_old_plan(self):
        sub = self.resolve([segment('starter', '2026-09-01', '2026-10-01'), segment('scale', '2026-10-01', '2026-11-01')])
        self.assertEqual([(s.plan_id, s.days) for s in sub.segments], [('starter', 30)])
        self.assertEqual(sub.period_end_plan_id, 'starter')

    def test_segment_order_in_input_does_not_matter(self):
        forward = [segment('starter', '2026-09-01', '2026-09-17'), segment('scale', '2026-09-17', '2026-10-01')]
        self.assertEqual(self.resolve(forward), self.resolve(list(reversed(forward))))

    def test_subscription_needs_no_usage_events(self):
        # Rule 9: an account with no usage still gets a subscription; resolution takes only
        # the account, the period and the plans, never events.
        import inspect
        from rvn_ledger.subscription import account_subscription
        self.assertEqual(list(inspect.signature(account_subscription).parameters), ['account', 'period', 'plans'])
        sub = self.resolve([segment('starter', '2026-09-01', '2026-10-01')])
        self.assertEqual(sum(s.amount_minor for s in sub.segments), 2900)

    def test_gap_inside_period_charges_only_covered_days(self):
        # Documented assumption: an uncovered stretch of days carries no plan and no charge;
        # the period end must still be covered so usage can be priced.
        sub = self.resolve([segment('starter', '2026-09-01', '2026-09-10'), segment('scale', '2026-09-20', '2026-10-01')])
        self.assertEqual([(s.plan_id, s.days) for s in sub.segments], [('starter', 9), ('scale', 11)])
        self.assertEqual(sum(s.days for s in sub.segments), 20)
        self.assertEqual(sub.period_end_plan_id, 'scale')

    def test_fee_is_looked_up_in_the_account_currency(self):
        usd = self.resolve([segment('starter', '2026-09-01', '2026-10-01')], 'USD')
        eur = self.resolve([segment('starter', '2026-09-01', '2026-10-01')], 'EUR')
        self.assertEqual((usd.currency, usd.segments[0].fee_minor, usd.segments[0].amount_minor), ('USD', 2900, 2900))
        self.assertEqual((eur.currency, eur.segments[0].fee_minor, eur.segments[0].amount_minor), ('EUR', 2700, 2700))

    def test_missing_currency_price_fails_closed(self):
        with self.assertRaises(InputError):
            self.resolve([segment('usd_only', '2026-09-01', '2026-10-01')], 'EUR')
        for currency in (None, 5, '', 'try'):
            with self.subTest(currency=currency), self.assertRaises(InputError):
                self.resolve([segment('starter', '2026-09-01', '2026-10-01')], currency)

    def test_no_plan_at_period_end_fails_closed(self):
        # The segment ending on the final local day (to = 2026-09-30, exclusive) does not cover
        # that day; only a segment with to >= 2026-10-01 can be the period-end plan.
        for segments in ([segment('starter', '2026-09-01', '2026-09-25')], [segment('starter', '2026-09-01', '2026-09-30')],
                         [], [segment('starter', '2026-07-01', '2026-08-01')]):
            with self.subTest(segments=segments), self.assertRaises(InputError):
                self.resolve(segments)

    def test_overlapping_segments_fail_closed(self):
        for segments in ([segment('starter', '2026-09-01', '2026-09-20'), segment('scale', '2026-09-17', '2026-10-01')],
                         [segment('starter', '2026-09-01', '2026-10-01'), segment('scale', '2026-09-01', '2026-10-01')],
                         [segment('starter', '2026-09-01', '2026-10-01'), segment('scale', '2026-09-30', '2026-10-02')]):
            with self.subTest(segments=segments), self.assertRaises(InputError):
                self.resolve(segments)

    def test_invalid_segment_definitions_fail_closed(self):
        full = segment('starter', '2026-09-01', '2026-10-01')
        bad = [segment('starter', '2026-09-17', '2026-09-17'), segment('starter', '2026-09-17', '2026-09-01'),
               segment('unknown', '2026-09-01', '2026-10-01'), segment('starter', 'bad', '2026-10-01'),
               segment('starter', '2026-09-01', '20261001'), segment('starter', 20260901, '2026-10-01'),
               segment('', '2026-09-01', '2026-10-01'), segment(None, '2026-09-01', '2026-10-01'),
               {'plan_id': 'starter', 'from': '2026-09-01'}, 'starter']
        for entry in bad:
            with self.subTest(entry=entry), self.assertRaises(InputError):
                self.resolve([entry])
        for segments in ('segments', None, {'plan_id': 'starter'}):
            with self.subTest(segments=segments), self.assertRaises(InputError):
                self.resolve(segments)
        for plans in ({}, {'starter': {'plan_id': 'other', 'prices': PLANS['starter']['prices']}},
                      {'starter': {'plan_id': 'starter'}}, {'starter': {'plan_id': 'starter', 'prices': {'USD': {}}}}, []):
            with self.subTest(plans=plans), self.assertRaises(InputError):
                self.resolve([full], plans=plans)

    def test_non_money_fee_fails_closed(self):
        for fee in (True, 29.0, -1, '2900', None):
            plans = {'starter': {'plan_id': 'starter', 'prices': {'USD': {'subscription_fee_minor': fee}}}}
            with self.subTest(fee=fee), self.assertRaises(InputError):
                self.resolve([segment('starter', '2026-09-01', '2026-10-01')], plans=plans)

    def test_period_days_must_match_local_dates(self):
        full = [segment('starter', '2026-09-01', '2026-10-01')]
        for override in ({'days_in_period': 31}, {'days_in_period': True}, {'days_in_period': 30.0}, {'days_in_period': '30'},
                         {'period_end_local_exclusive': '2026-09-01'}, {'period_start_local': 'bad'}):
            with self.subTest(override=override), self.assertRaises(InputError):
                self.resolve(full, period={**PERIOD, **override})
        with self.assertRaises(InputError):
            self.resolve(full, period={k: v for k, v in PERIOD.items() if k != 'days_in_period'})

    def test_days_are_whole_local_calendar_days_across_dst(self):
        # Review F4: the old October period spanned exactly 31 x 24 elapsed hours (DST ends after
        # the exclusive end), so it could not tell calendar days from hours / 24. America/New_York
        # enters DST on 2026-03-08: March has 31 calendar days but only 30 days and 23 hours of
        # elapsed time, so hours / 24 would give 30. Whole local days must count the calendar.
        from datetime import datetime, timedelta, timezone
        from zoneinfo import ZoneInfo
        from rvn_ledger.subscription import account_subscription
        zone = ZoneInfo('America/New_York')
        start, end = datetime(2026, 3, 1, tzinfo=zone), datetime(2026, 4, 1, tzinfo=zone)
        elapsed = end.astimezone(timezone.utc) - start.astimezone(timezone.utc)
        self.assertEqual(elapsed, timedelta(days=30, hours=23))   # the period really is one hour short
        self.assertEqual(elapsed // timedelta(hours=24), 30)       # what an hours / 24 implementation would count
        period = {**PERIOD, 'period_start_local': '2026-03-01', 'period_end_local_exclusive': '2026-04-01', 'days_in_period': 31}
        sub = account_subscription(account([segment('starter', '2026-03-01', '2026-04-01')], zone='America/New_York'), period, PLANS)
        self.assertEqual([(s.days, s.amount_minor) for s in sub.segments], [(31, 2900)])
        self.assertEqual(sub.segments[0].start, date(2026, 3, 1))
        # A segment boundary on the DST day itself: 8 calendar days before, 23 after.
        split = account_subscription(account([segment('starter', '2026-03-01', '2026-03-09'), segment('scale', '2026-03-09', '2026-04-01')],
                                             zone='America/New_York'), period, PLANS)
        self.assertEqual([(s.plan_id, s.days) for s in split.segments], [('starter', 8), ('scale', 23)])
        self.assertEqual([s.amount_minor for s in split.segments], [reference_amount(2900, 8, 31), reference_amount(29900, 23, 31)])

    def test_all_accounts_resolved_eagerly_and_sorted(self):
        from rvn_ledger.subscription import all_subscriptions
        accounts = [account([segment('starter', '2026-09-01', '2026-10-01')], account_id='b'),
                    account([segment('scale', '2026-09-01', '2026-10-01')], 'EUR', account_id='a')]
        result = all_subscriptions(accounts, PERIOD, PLANS)
        self.assertEqual(list(result), ['a', 'b'])
        self.assertEqual(result['a'].segments[0].amount_minor, 27900)
        broken = accounts + [account([segment('starter', '2026-09-01', '2026-09-20')], account_id='c')]
        with self.assertRaises(InputError):
            all_subscriptions(broken, PERIOD, PLANS)
        with self.assertRaises(InputError):
            all_subscriptions(accounts + [account([segment('starter', '2026-09-01', '2026-10-01')], account_id='a')], PERIOD, PLANS)
        for bad in ('accounts', None, [{'timezone': 'UTC'}], ['x']):
            with self.subTest(bad=bad), self.assertRaises(InputError):
                all_subscriptions(bad, PERIOD, PLANS)

    def test_days_never_exceed_period_and_total_fee_conserved_property(self):
        # For every split point of a full-coverage two-segment history with the same plan, the
        # segment days sum to the period length; the rounded amounts sum to within one minor
        # unit of the full fee (per-line rounding), never to a float.
        for split in range(2, 30):
            day = date(2026, 9, split).isoformat()
            sub = self.resolve([segment('starter', '2026-09-01', day), segment('starter', day, '2026-10-01')])
            with self.subTest(split=split):
                self.assertEqual(sum(s.days for s in sub.segments), 30)
                self.assertTrue(all(0 < s.days <= 30 for s in sub.segments))
                self.assertLessEqual(abs(sum(s.amount_minor for s in sub.segments) - 2900), 1)
