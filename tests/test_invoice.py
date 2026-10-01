import json
import unittest

from rvn_ledger.inputs import InputError

from fixtures import ACCOUNTS, build, EXPECTED_INVOICES, PLANS, reference_line


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


