"""One test per billing rule / input hazard, end to end through `run_ledger`.

Read this file first: each test names the rule it pins and uses inputs small
enough to check by hand. The hand-verified fixture in `fixtures.py` combines all
of them in one run (tests/test_invoice.py, tests/test_pipeline.py).

    rule 1  period & timezone          test_rule1_...
    rule 2  deduplication              test_rule2_...
    rule 3  late arrivals              test_rule3_...
    rule 4  quarantine, never crash    test_rule4_...
    rule 5  subscription proration     test_rule5_...
    rule 6  usage tiers                test_rule6_...
    rule 7  money and rounding         test_rule7_...
    rule 8  credits                    test_rule8_...
    rule 9  currency, no-usage invoice test_rule9_...
    input   out-of-order delivery      test_events_out_of_order_...

Idempotency (byte-identical reruns) is pinned in tests/test_pipeline.py and the
invariants (total = sum of lines - credit, never negative, ...) in
tests/test_invariants.py.
"""
import copy
import json
import unittest

from rvn_ledger.inputs import InputError
from rvn_ledger.pipeline import run_ledger

from fixtures import ACCOUNTS, EVENTS, EXPECTED_INVOICES, PERIOD, PLANS, account, event, raw_lines


def run(events, accounts=ACCOUNTS, plans=PLANS, period=PERIOD):
    return run_ledger({'events.jsonl': raw_lines(events), 'accounts.json': json.dumps(accounts).encode(),
                       'plans.json': json.dumps(plans).encode(), 'period.json': json.dumps(period).encode()})


def invoice(result, account_id):
    return next(inv for inv in result.invoices if inv['account_id'] == account_id)


def statuses(result):
    return {d['event_id']: d['status'] for d in result.audit['decisions']}


def usage(result, account_id):
    return [(l['metric'], l['tier_from'], l['tier_to'], l['units'], l['amount_minor'])
            for l in invoice(result, account_id)['lines'] if l['kind'] == 'usage']


class HazardTests(unittest.TestCase):
    def test_rule1_period_is_half_open_in_each_accounts_local_timezone(self):
        # acct_d is Asia/Tokyo (UTC+9), acct_b America/New_York (UTC-4 in September).
        result = run([
            event('tokyo-before', 1, 'acct_d', units=1, ts='2026-08-31T14:59:59Z'),   # Aug 31 23:59:59 local
            event('tokyo-start', 2, 'acct_d', units=2, ts='2026-08-31T15:00:00Z'),    # Sep 1 00:00:00 local: start is inclusive
            event('tokyo-last', 3, 'acct_d', units=4, ts='2026-09-30T14:59:59Z', ingested_at='2026-09-30T15:30:00Z'),
            event('tokyo-end', 4, 'acct_d', units=8, ts='2026-09-30T15:00:00Z', ingested_at='2026-09-30T15:30:00Z'),  # end is exclusive
            event('ny-before', 5, 'acct_b', units=16, ts='2026-09-01T03:59:59Z'),
            event('ny-last', 6, 'acct_b', units=32, ts='2026-10-01T03:59:59Z', ingested_at='2026-10-01T04:30:00Z'),
        ])
        self.assertEqual(statuses(result), {
            'tokyo-before': 'excluded_out_of_period', 'tokyo-start': 'accepted', 'tokyo-last': 'accepted',
            'tokyo-end': 'excluded_out_of_period', 'ny-before': 'excluded_out_of_period', 'ny-last': 'accepted'})
        self.assertEqual(invoice(result, 'acct_d')['billable_units']['api_calls'], 6)
        self.assertEqual(invoice(result, 'acct_b')['billable_units']['api_calls'], 32)

    def test_rule2_first_copy_by_ingest_seq_wins_even_with_a_different_payload(self):
        result = run([
            event('dup', 7, 'acct_a', units=5),                     # line 1: arrives first, but a later sequence
            event('dup', 3, 'acct_a', units=9),                     # line 2: the first copy by ingest_seq wins
            event('dup', 9, 'acct_b', units=1000),                  # line 3: different account and units, still a duplicate
        ])
        by_line = {d['line']: d for d in result.audit['decisions']}
        self.assertEqual(by_line[2]['status'], 'accepted')
        self.assertEqual((by_line[1]['status'], by_line[1]['canonical_line']), ('duplicate_ignored', 2))
        self.assertEqual((by_line[3]['status'], by_line[3]['canonical_line']), ('duplicate_ignored', 2))
        self.assertEqual(invoice(result, 'acct_a')['billable_units']['api_calls'], 9)
        self.assertEqual(invoice(result, 'acct_b')['billable_units']['api_calls'], 0)

    def test_rule3_late_arrivals_more_than_48h_after_the_local_period_end_are_excluded(self):
        # Istanbul period end = 2026-09-30T21:00Z, cutoff 2026-10-02T21:00Z.
        # New York period end = 2026-10-01T04:00Z, cutoff 2026-10-03T04:00Z (a UTC cutoff would be 4h earlier).
        result = run([
            event('ist-at-cutoff', 1, 'acct_a', units=1, ts='2026-09-30T20:00:00Z', ingested_at='2026-10-02T21:00:00Z'),
            event('ist-late', 2, 'acct_a', units=2, ts='2026-09-30T20:00:00Z', ingested_at='2026-10-02T21:00:01Z'),
            event('ny-in-time', 3, 'acct_b', units=4, ts='2026-10-01T03:00:00Z', ingested_at='2026-10-03T03:59:59Z'),
            event('ny-late', 4, 'acct_b', units=8, ts='2026-10-01T03:00:00Z', ingested_at='2026-10-03T04:00:01Z'),
        ])
        self.assertEqual(statuses(result), {'ist-at-cutoff': 'accepted', 'ist-late': 'excluded_late',
                                            'ny-in-time': 'accepted', 'ny-late': 'excluded_late'})
        self.assertEqual(invoice(result, 'acct_a')['billable_units']['api_calls'], 1)
        self.assertEqual(invoice(result, 'acct_b')['billable_units']['api_calls'], 4)
        self.assertEqual(result.quarantine, [])          # late is excluded, not quarantined

    def test_rule4_bad_records_are_quarantined_with_a_reason_and_the_run_continues(self):
        missing_units = {k: v for k, v in event('q-missing-units', 1, 'acct_a').items() if k != 'units'}
        result = run([
            missing_units,
            event('q-text-units', 2, 'acct_a', units='12'),
            event('q-float-units', 3, 'acct_a', units=1.5),
            event('q-metric', 4, 'acct_a', metric='gpu_hours'),
            event('q-ts', 5, 'acct_a', ts='not-a-timestamp'),
            event('q-ingested', 6, 'acct_a', ingested_at='2026-13-01T00:00:00Z'),
            event('q-zero', 7, 'acct_a', units=0),
            event('q-negative', 8, 'acct_a', units=-5),
            event('q-account', 9, 'acct_missing'),
            '{broken',
            event('good', 10, 'acct_a', units=7),
        ])
        self.assertEqual(result.quarantine, [
            {'event_id': 'q-missing-units', 'reason': 'invalid_units'},
            {'event_id': 'q-text-units', 'reason': 'invalid_units'},
            {'event_id': 'q-float-units', 'reason': 'invalid_units'},
            {'event_id': 'q-metric', 'reason': 'unknown_metric'},
            {'event_id': 'q-ts', 'reason': 'invalid_ts'},
            {'event_id': 'q-ingested', 'reason': 'invalid_ingested_at'},
            {'event_id': 'q-zero', 'reason': 'nonpositive_units'},
            {'event_id': 'q-negative', 'reason': 'nonpositive_units'},
            {'event_id': 'q-account', 'reason': 'unknown_account'},
            {'event_id': None, 'reason': 'invalid_json'},
        ])
        self.assertEqual(len(result.invoices), len(ACCOUNTS))                  # every account still invoiced
        self.assertEqual(invoice(result, 'acct_a')['billable_units']['api_calls'], 7)
        self.assertEqual(invoice(result, 'acct_a')['quarantined_count'], 8)   # the unknown account and broken line belong to nobody

    def test_rule5_subscription_is_prorated_by_whole_local_days_per_plan_segment(self):
        plans = copy.deepcopy(PLANS)
        plans['half'] = {'plan_id': 'half', 'prices': {'USD': {'subscription_fee_minor': 45, 'metrics': {}}}}
        accounts = ACCOUNTS + [account('acct_h', 'UTC', 'USD', 0, [('half', '2026-09-01', '2026-09-02'),
                                                                   ('starter', '2026-09-02', '2026-10-01')])]
        result = run([], accounts=accounts, plans=plans)
        subscription = lambda account_id: [(l['plan_id'], l['days'], l['amount_minor'])
                                           for l in invoice(result, account_id)['lines'] if l['kind'] == 'subscription']
        self.assertEqual(subscription('acct_d'), [('growth', 30, 9900)])                         # all 30 days: the full fee
        self.assertEqual(subscription('acct_b'), [('starter', 16, 1547), ('growth', 14, 4620)])   # 2900*16/30 = 1546.67
        self.assertEqual(subscription('acct_h'), [('half', 1, 2), ('starter', 29, 2803)])         # 45*1/30 = 1.5 rounds up

    def test_rule6_usage_is_priced_on_the_period_end_plan_with_cumulative_brackets(self):
        # acct_b moves starter -> growth on 2026-09-17; all its usage is priced on growth.
        plan_change = run([event('b', 1, 'acct_b', units=60000)])
        self.assertEqual(usage(plan_change, 'acct_b'), [('api_calls', 0, 50000, 50000, 9500), ('api_calls', 50000, 250000, 10000, 1400)])
        # Brackets are [from, to): exactly 10000 units fill the first bracket and nothing spills over.
        self.assertEqual(usage(run([event('a', 1, 'acct_a', units=10000)]), 'acct_a'), [('api_calls', 0, 10000, 10000, 86000)])
        # Cumulative over the whole period, not per event: 6000 + 6000 crosses into the second bracket.
        split = run([event('a1', 1, 'acct_a', units=6000), event('a2', 2, 'acct_a', units=6000)])
        self.assertEqual(usage(split, 'acct_a'), [('api_calls', 0, 10000, 10000, 86000), ('api_calls', 10000, None, 2000, 12200)])

    def test_rule7_each_line_rounds_half_up_and_the_subtotal_sums_rounded_lines(self):
        # USD starter: 2 api_calls * 2500 micros = 0.5 minor, 4 storage units * 1250 micros = 0.5 minor.
        accounts = ACCOUNTS + [account('acct_r', 'UTC', 'USD', 0, [('starter', '2026-09-01', '2026-10-01')])]
        result = run([event('r1', 1, 'acct_r', units=2), event('r2', 2, 'acct_r', metric='storage_gb_hours', units=4)], accounts=accounts)
        inv = invoice(result, 'acct_r')
        self.assertEqual(usage(result, 'acct_r'), [('api_calls', 0, 10000, 2, 1), ('storage_gb_hours', 0, None, 4, 1)])
        self.assertEqual(inv['subtotal_minor'], 2900 + 1 + 1)     # rounding the sum (0.5 + 0.5) would give 2901

        def no_floats(node):
            self.assertNotIsInstance(node, float)
            for child in (node.values() if isinstance(node, dict) else node if isinstance(node, list) else ()):
                no_floats(child)
        no_floats(result.invoices)

    def test_rule8_credit_is_capped_at_the_subtotal_and_the_rest_is_reported(self):
        result = run(EVENTS)
        money = lambda account_id: tuple(invoice(result, account_id)[k] for k in
                                         ('subtotal_minor', 'credit_applied_minor', 'credit_remaining_minor', 'total_minor'))
        self.assertEqual(money('acct_c'), (27900, 27900, 72100, 0))        # credit 100000 > subtotal: total 0, never negative
        self.assertEqual(money('acct_a'), (198551, 500, 0, 198051))        # credit 500 < subtotal: fully used

    def test_rule9_currencies_are_never_mixed_and_an_account_without_usage_still_gets_an_invoice(self):
        result = run(EVENTS)
        c = invoice(result, 'acct_c')                                       # EUR, no usage at all
        self.assertEqual((c['currency'], [l['kind'] for l in c['lines']]), ('EUR', ['subscription']))
        self.assertEqual(sorted(result.manifest['totals_by_currency']), ['EUR', 'TRY', 'USD'])   # totals never mixed
        # No exchange rate is ever invented: a currency the plan does not price stops the run.
        accounts = ACCOUNTS + [account('acct_gbp', 'UTC', 'GBP', 0, [('starter', '2026-09-01', '2026-10-01')])]
        with self.assertRaisesRegex(InputError, 'GBP'):
            run(EVENTS, accounts=accounts)

    def test_events_out_of_order_give_the_same_invoices(self):
        self.assertEqual(run(EVENTS).invoices, EXPECTED_INVOICES)
        self.assertEqual(run(list(reversed(EVENTS))).invoices, EXPECTED_INVOICES)
