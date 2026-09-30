import json
import random
import unittest
from rvn_ledger.inputs import InputError, read_events
from test_tiers import PLANS as TIER_PLANS, reference_line

PLANS = json.loads(json.dumps(TIER_PLANS))
PLANS['scale'] = {'plan_id': 'scale', 'prices': {'EUR': {'subscription_fee_minor': 27900, 'metrics': {
    'api_calls': [{'from_units': 0, 'to_units': 250000, 'unit_price_micros': 1100}, {'from_units': 250000, 'to_units': None, 'unit_price_micros': 650}],
    'storage_gb_hours': [{'from_units': 0, 'to_units': None, 'unit_price_micros': 600}]}}}}
PLANS['starter']['prices']['EUR'] = {'subscription_fee_minor': 2700, 'metrics': {
    'api_calls': [{'from_units': 0, 'to_units': 10000, 'unit_price_micros': 2300}, {'from_units': 10000, 'to_units': None, 'unit_price_micros': 1650}],
    'storage_gb_hours': [{'from_units': 0, 'to_units': None, 'unit_price_micros': 1150}]}}
PERIOD = {'period_start_local': '2026-09-01', 'period_end_local_exclusive': '2026-10-01', 'days_in_period': 30,
          'late_cutoff_hours_after_period_end': 48, 'metrics': ['api_calls', 'storage_gb_hours']}


def account(account_id, zone, currency, credit, segments):
    return {'account_id': account_id, 'name': account_id, 'timezone': zone, 'currency': currency, 'credit_minor': credit,
            'plan_segments': [{'plan_id': p, 'from': f, 'to': t} for p, f, t in segments]}


def event(event_id, seq, account_id, metric='api_calls', units=1, ts='2026-09-10T00:00:00Z', ingested_at='2026-09-10T00:01:00Z', **extra):
    return {'event_id': event_id, 'ingest_seq': seq, 'account_id': account_id, 'metric': metric, 'units': units,
            'ts': ts, 'ingested_at': ingested_at, **extra}


def raw_lines(values):
    return ('\n'.join(v if isinstance(v, str) else json.dumps(v) for v in values) + '\n').encode()


# Hand-verified hazard fixture (see tests/test_pipeline.py for the full expected outputs):
#   acct_a Europe/Istanbul TRY starter, credit 500     - usage crossing a tier, duplicate with different payload,
#                                                        out-of-period, late, exact late boundary, two quarantines
#   acct_b America/New_York USD starter->growth 09-17  - plan change mid period, usage priced on growth
#   acct_c UTC EUR scale, credit 100000                - no usage, credit exceeds subtotal
#   acct_d Asia/Tokyo USD growth, credit 1000          - storage across tiers, api line rounding to 1 minor unit
ACCOUNTS = [
    account('acct_a', 'Europe/Istanbul', 'TRY', 500, [('starter', '2026-09-01', '2026-10-01')]),
    account('acct_b', 'America/New_York', 'USD', 0, [('starter', '2026-09-01', '2026-09-17'), ('growth', '2026-09-17', '2026-10-01')]),
    account('acct_c', 'UTC', 'EUR', 100000, [('scale', '2026-09-01', '2026-10-01')]),
    account('acct_d', 'Asia/Tokyo', 'USD', 1000, [('growth', '2026-09-01', '2026-10-01')]),
]
EVENTS = [
    event('a1', 1, 'acct_a', units=4000),                                                                      # 1 accepted
    event('a2', 5, 'acct_a', units=8000, ts='2026-09-20T12:00:00Z', ingested_at='2026-09-20T12:00:00Z'),        # 2 accepted (seq 5 < line 3's 9)
    event('a2', 9, 'acct_a', units=999999),                                                                     # 3 duplicate, different payload
    event('a3', 3, 'acct_a', metric='storage_gb_hours', units=100),                                             # 4 accepted
    event('a4', 4, 'acct_a', units=5, ts='2026-08-31T20:59:59Z'),                                               # 5 out of period (Istanbul 23:59:59 Aug 31)
    event('a5', 6, 'acct_a', units=5, ts='2026-09-30T20:59:59Z', ingested_at='2026-10-02T21:00:01Z'),          # 6 late by one second
    event('a6', 7, 'acct_a', units=5, ts='2026-09-30T20:59:59Z', ingested_at='2026-10-02T21:00:00Z'),          # 7 accepted: exactly 48h
    event('a7', 8, 'acct_a', units=0),                                                                          # 8 quarantined nonpositive_units
    event('a8', 10, 'acct_a', units='12'),                                                                      # 9 quarantined invalid_units
    event('b1', 12, 'acct_b', units=50000),                                                                     # 10 accepted
    event('b2', 11, 'acct_b', units=10000, ts='2026-09-01T04:00:00Z'),                                          # 11 accepted: NY period start exactly
    event('b3', 13, 'acct_b', metric='gpu', units=7),                                                           # 12 quarantined unknown_metric
    event('z1', 14, 'acct_zzz', units=7),                                                                       # 13 quarantined unknown_account
    event('d1', 16, 'acct_d', metric='storage_gb_hours', units=7500),                                           # 14 accepted
    event('d2', 15, 'acct_d', units=3),                                                                         # 15 accepted (out of order, fine)
    event('d3', 17, 'acct_d', units=-5),                                                                        # 16 quarantined nonpositive_units
    event('c1', 18, 'acct_c', units=4, ts='2026-09-10 00:00:00'),                                               # 17 quarantined invalid_ts
    '{broken',                                                                                                  # 18 quarantined invalid_json, no identity
]
RAW_EVENTS = raw_lines(EVENTS)

EXPECTED_INVOICES = [
    {'account_id': 'acct_a', 'currency': 'TRY', 'timezone': 'Europe/Istanbul',
     'billable_units': {'api_calls': 12005, 'storage_gb_hours': 100},
     'lines': [{'kind': 'subscription', 'plan_id': 'starter', 'days': 30, 'amount_minor': 99900},
               {'kind': 'usage', 'metric': 'api_calls', 'tier_from': 0, 'tier_to': 10000, 'units': 10000, 'amount_minor': 86000},
               {'kind': 'usage', 'metric': 'api_calls', 'tier_from': 10000, 'tier_to': None, 'units': 2005, 'amount_minor': 12231},
               {'kind': 'usage', 'metric': 'storage_gb_hours', 'tier_from': 0, 'tier_to': None, 'units': 100, 'amount_minor': 420}],
     'subtotal_minor': 198551, 'credit_applied_minor': 500, 'credit_remaining_minor': 0, 'total_minor': 198051, 'quarantined_count': 2},
    {'account_id': 'acct_b', 'currency': 'USD', 'timezone': 'America/New_York',
     'billable_units': {'api_calls': 60000, 'storage_gb_hours': 0},
     'lines': [{'kind': 'subscription', 'plan_id': 'starter', 'days': 16, 'amount_minor': 1547},
               {'kind': 'subscription', 'plan_id': 'growth', 'days': 14, 'amount_minor': 4620},
               {'kind': 'usage', 'metric': 'api_calls', 'tier_from': 0, 'tier_to': 50000, 'units': 50000, 'amount_minor': 9500},
               {'kind': 'usage', 'metric': 'api_calls', 'tier_from': 50000, 'tier_to': 250000, 'units': 10000, 'amount_minor': 1400}],
     'subtotal_minor': 17067, 'credit_applied_minor': 0, 'credit_remaining_minor': 0, 'total_minor': 17067, 'quarantined_count': 1},
    {'account_id': 'acct_c', 'currency': 'EUR', 'timezone': 'UTC',
     'billable_units': {'api_calls': 0, 'storage_gb_hours': 0},
     'lines': [{'kind': 'subscription', 'plan_id': 'scale', 'days': 30, 'amount_minor': 27900}],
     'subtotal_minor': 27900, 'credit_applied_minor': 27900, 'credit_remaining_minor': 72100, 'total_minor': 0, 'quarantined_count': 1},
    {'account_id': 'acct_d', 'currency': 'USD', 'timezone': 'Asia/Tokyo',
     'billable_units': {'api_calls': 3, 'storage_gb_hours': 7500},
     'lines': [{'kind': 'subscription', 'plan_id': 'growth', 'days': 30, 'amount_minor': 9900},
               {'kind': 'usage', 'metric': 'api_calls', 'tier_from': 0, 'tier_to': 50000, 'units': 3, 'amount_minor': 1},
               {'kind': 'usage', 'metric': 'storage_gb_hours', 'tier_from': 0, 'tier_to': 5000, 'units': 5000, 'amount_minor': 550},
               {'kind': 'usage', 'metric': 'storage_gb_hours', 'tier_from': 5000, 'tier_to': None, 'units': 2500, 'amount_minor': 200}],
     'subtotal_minor': 10651, 'credit_applied_minor': 1000, 'credit_remaining_minor': 0, 'total_minor': 9651, 'quarantined_count': 1},
]
EXPECTED_QUARANTINE = [
    {'event_id': 'a7', 'reason': 'nonpositive_units'}, {'event_id': 'a8', 'reason': 'invalid_units'},
    {'event_id': 'b3', 'reason': 'unknown_metric'}, {'event_id': 'z1', 'reason': 'unknown_account'},
    {'event_id': 'd3', 'reason': 'nonpositive_units'}, {'event_id': 'c1', 'reason': 'invalid_ts'},
    {'event_id': None, 'reason': 'invalid_json'},
]


def build(accounts=ACCOUNTS, raw=RAW_EVENTS, period=PERIOD, plans=PLANS):
    from rvn_ledger.invoice import build_invoices
    from rvn_ledger.selection import classify_events
    rows = read_events(raw)
    classification = classify_events(rows, accounts, period)
    return build_invoices(accounts, period, plans, rows, classification)


class InvoiceTests(unittest.TestCase):
    def test_hand_verified_invoices_for_every_hazard(self):
        self.assertEqual([inv.as_contract() for inv in build()], EXPECTED_INVOICES)

    def test_contract_shape_and_key_order_exactly(self):
        contract_keys = ['account_id', 'currency', 'timezone', 'billable_units', 'lines', 'subtotal_minor',
                         'credit_applied_minor', 'credit_remaining_minor', 'total_minor', 'quarantined_count']
        for inv in build():
            document = inv.as_contract()
            self.assertEqual(list(document), contract_keys)
            self.assertEqual(list(document['billable_units']), ['api_calls', 'storage_gb_hours'])
            for line in document['lines']:
                if line['kind'] == 'subscription':
                    self.assertEqual(list(line), ['kind', 'plan_id', 'days', 'amount_minor'])
                else:
                    self.assertEqual(list(line), ['kind', 'metric', 'tier_from', 'tier_to', 'units', 'amount_minor'])
                    self.assertGreater(line['units'], 0)
            for key in ('subtotal_minor', 'credit_applied_minor', 'credit_remaining_minor', 'total_minor', 'quarantined_count'):
                self.assertIs(type(document[key]), int)
            self.assertIn(document['currency'], ('USD', 'EUR', 'TRY'))

    def test_invoices_sorted_by_account_id_regardless_of_input_order(self):
        forward = [inv.account_id for inv in build()]
        self.assertEqual(forward, sorted(forward))
        self.assertEqual([inv.as_contract() for inv in build(accounts=list(reversed(ACCOUNTS)))], EXPECTED_INVOICES)

    def test_no_usage_account_gets_subscription_only_invoice_with_credit_rule(self):
        c = [inv for inv in build() if inv.account_id == 'acct_c'][0].as_contract()
        self.assertEqual([l['kind'] for l in c['lines']], ['subscription'])
        self.assertEqual((c['subtotal_minor'], c['credit_applied_minor'], c['credit_remaining_minor'], c['total_minor']), (27900, 27900, 72100, 0))

    def test_usage_is_priced_on_the_period_end_plan(self):
        b = [inv for inv in build() if inv.account_id == 'acct_b'][0].as_contract()
        usage = [l for l in b['lines'] if l['kind'] == 'usage']
        self.assertEqual([(l['tier_from'], l['tier_to'], l['amount_minor']) for l in usage], [(0, 50000, 9500), (50000, 250000, 1400)])
        # Under starter (USD) the same 60000 units would have been 2500 + 9000 = 11500, not 10900.
        self.assertEqual(reference_line(10000, 2500) + reference_line(50000, 1800), 11500)
        self.assertEqual(sum(l['amount_minor'] for l in usage), 10900)

    def test_total_is_sum_of_rounded_lines_minus_capped_credit_never_negative(self):
        for inv in build():
            document = inv.as_contract()
            with self.subTest(account=inv.account_id):
                self.assertEqual(document['subtotal_minor'], sum(l['amount_minor'] for l in document['lines']))
                self.assertEqual(document['total_minor'], document['subtotal_minor'] - document['credit_applied_minor'])
                self.assertGreaterEqual(document['total_minor'], 0)
                self.assertLessEqual(document['credit_applied_minor'], document['subtotal_minor'])

    def test_credit_variants(self):
        # zero, partial, equal to the subtotal, exceeding it: acct_d subtotal is 10651
        for credit, expected in [(0, (0, 0, 10651)), (1000, (1000, 0, 9651)), (10651, (10651, 0, 0)), (10652, (10651, 1, 0)), (10**9, (10651, 10**9 - 10651, 0))]:
            accounts = [a if a['account_id'] != 'acct_d' else {**a, 'credit_minor': credit} for a in ACCOUNTS]
            d = [inv for inv in build(accounts=accounts) if inv.account_id == 'acct_d'][0].as_contract()
            with self.subTest(credit=credit):
                self.assertEqual((d['credit_applied_minor'], d['credit_remaining_minor'], d['total_minor']), expected)

    def test_quarantined_count_counts_only_attributable_records(self):
        counts = {inv.account_id: inv.as_contract()['quarantined_count'] for inv in build()}
        self.assertEqual(counts, {'acct_a': 2, 'acct_b': 1, 'acct_c': 1, 'acct_d': 1})   # unknown-account and broken lines belong to nobody
        self.assertEqual(sum(counts.values()), 5)

    def test_currencies_are_never_mixed(self):
        for inv in build():
            expected = [a for a in ACCOUNTS if a['account_id'] == inv.account_id][0]['currency']
            self.assertEqual(inv.as_contract()['currency'], expected)
        missing_try = json.loads(json.dumps(PLANS))
        del missing_try['starter']['prices']['TRY']
        with self.assertRaises(InputError):
            build(plans=missing_try)
        # The subscription fee exists in TRY but the usage tariff does not: the tariff of another
        # currency must never be borrowed, even for a metric with zero usage.
        for metric in ('api_calls', 'storage_gb_hours'):
            tariff_missing = json.loads(json.dumps(PLANS))
            del tariff_missing['starter']['prices']['TRY']['metrics'][metric]
            with self.subTest(metric=metric), self.assertRaises(InputError):
                build(plans=tariff_missing)

    def test_bad_credit_or_account_configuration_fails_closed(self):
        for bad in ({'credit_minor': -1}, {'credit_minor': True}, {'credit_minor': 5.0}, {'credit_minor': '5'}, {'credit_minor': None},
                    {'currency': 'GBP'}, {'timezone': 'Mars/Olympus'}):
            accounts = [ACCOUNTS[0] | bad] + ACCOUNTS[1:]
            with self.subTest(bad=bad), self.assertRaises(InputError):
                build(accounts=accounts)
        without_credit = [{k: v for k, v in ACCOUNTS[0].items() if k != 'credit_minor'}] + ACCOUNTS[1:]
        with self.assertRaises(InputError):
            build(accounts=without_credit)

    def test_invoice_key_order_survives_serialisation(self):
        document = build()[0].as_contract()
        self.assertEqual(list(json.loads(json.dumps(document))), list(document))


class InvoicePropertyTests(unittest.TestCase):
    """Generated inputs, fixed seed; failures shrink into the hand-verified fixture above."""
    rng = random.Random(20260930)

    def generate(self, n=40):
        accounts = ['acct_a', 'acct_b', 'acct_d']
        values, seq = [], 1
        for i in range(n):
            acct = self.rng.choice(accounts)
            values.append(event(f'g{i}', seq, acct, self.rng.choice(['api_calls', 'storage_gb_hours']), self.rng.randrange(1, 60000),
                                ts=f'2026-09-{self.rng.randrange(2, 29):02d}T{self.rng.randrange(0, 24):02d}:00:00Z'))
            seq += 1
        return values, seq

    def test_later_duplicate_never_changes_an_invoice(self):
        for _ in range(10):
            values, seq = self.generate()
            base = [inv.as_contract() for inv in build(raw=raw_lines(values))]
            victim = self.rng.choice(values)
            duplicate = {**victim, 'ingest_seq': seq, 'units': 10 ** 6, 'account_id': 'acct_b'}
            with self.subTest(victim=victim['event_id']):
                self.assertEqual([inv.as_contract() for inv in build(raw=raw_lines(values + [duplicate]))], base)
                self.assertEqual([inv.as_contract() for inv in build(raw=raw_lines([duplicate] + values))], base)

    def test_file_order_never_changes_an_invoice_when_sequences_are_unique(self):
        for _ in range(10):
            values, _ = self.generate()
            base = [inv.as_contract() for inv in build(raw=raw_lines(values))]
            shuffled = list(values)
            self.rng.shuffle(shuffled)
            self.assertEqual([inv.as_contract() for inv in build(raw=raw_lines(shuffled))], base)

    def test_one_accounts_events_never_change_another_invoice(self):
        for _ in range(10):
            values, seq = self.generate()
            base = {inv.account_id: inv.as_contract() for inv in build(raw=raw_lines(values))}
            extra = [event(f'x{i}', seq + i, 'acct_d', units=self.rng.randrange(1, 9000)) for i in range(5)]
            changed = {inv.account_id: inv.as_contract() for inv in build(raw=raw_lines(values + extra))}
            for account_id in ('acct_a', 'acct_b', 'acct_c'):
                self.assertEqual(changed[account_id], base[account_id])
            self.assertNotEqual(changed['acct_d'], base['acct_d'])

    def test_more_credit_never_increases_the_total(self):
        values, _ = self.generate()
        previous = None
        for credit in sorted(self.rng.randrange(0, 300000) for _ in range(20)):
            accounts = [a if a['account_id'] != 'acct_a' else {**a, 'credit_minor': credit} for a in ACCOUNTS]
            a = [inv for inv in build(accounts=accounts, raw=raw_lines(values)) if inv.account_id == 'acct_a'][0].as_contract()
            self.assertGreaterEqual(a['total_minor'], 0)
            self.assertEqual(a['total_minor'], a['subtotal_minor'] - a['credit_applied_minor'])
            if previous is not None:
                self.assertLessEqual(a['total_minor'], previous)
            previous = a['total_minor']

    def test_billable_units_equal_accepted_units_and_tier_units(self):
        values, _ = self.generate(80)
        for inv in build(raw=raw_lines(values)):
            document = inv.as_contract()
            for metric in PERIOD['metrics']:
                tier_units = sum(l['units'] for l in document['lines'] if l['kind'] == 'usage' and l['metric'] == metric)
                self.assertEqual(tier_units, document['billable_units'][metric])
