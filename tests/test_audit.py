import copy
import json
import unittest
from test_invoice import ACCOUNTS, EXPECTED_INVOICES, EXPECTED_QUARANTINE, PERIOD, PLANS, RAW_EVENTS


def run_fixture():
    from rvn_ledger.pipeline import run_ledger
    raw = {'events.jsonl': RAW_EVENTS, 'accounts.json': json.dumps(ACCOUNTS).encode(), 'plans.json': json.dumps(PLANS).encode(),
           'period.json': json.dumps(PERIOD).encode()}
    return run_ledger(raw)


class AuditTests(unittest.TestCase):
    def test_every_raw_line_has_one_decision_with_source_hash(self):
        run = run_fixture()
        decisions = run.audit['decisions']
        self.assertEqual([d['line'] for d in decisions], list(range(1, 19)))
        self.assertEqual({d['status'] for d in decisions} <= {'accepted', 'duplicate_ignored', 'excluded_out_of_period', 'excluded_late', 'quarantined'}, True)
        self.assertEqual(decisions[2], {'line': 3, 'sha256': decisions[2]['sha256'], 'event_id': 'a2', 'status': 'duplicate_ignored', 'canonical_line': 2})
        self.assertEqual(decisions[7]['reasons'], ['nonpositive_units'])
        self.assertEqual(decisions[17], {'line': 18, 'sha256': decisions[17]['sha256'], 'event_id': None, 'status': 'quarantined', 'reasons': ['invalid_json']})
        self.assertEqual(len(decisions[0]['sha256']), 64)
        counts = run.manifest['counts']
        self.assertEqual(counts['raw'], 18)
        self.assertEqual(sum(counts[k] for k in ('accepted', 'duplicate_ignored', 'excluded_out_of_period', 'excluded_late', 'quarantined')), 18)

    def test_every_usage_line_traces_to_events_whose_units_reproduce_it(self):
        run = run_fixture()
        invoices = {inv['account_id']: inv for inv in run.invoices}
        for account_id, trail in run.audit['invoices'].items():
            for metric, sources in trail['usage_sources'].items():
                total = sum(s['units'] for s in sources)
                self.assertEqual(total, invoices[account_id]['billable_units'][metric])
                lines = [l for l in trail['usage_lines'] if l['metric'] == metric]
                self.assertEqual(sum(l['units'] for l in lines), total)
                for line in lines:
                    self.assertEqual(line['plan_id'], trail['period_end_plan_id'])
                    self.assertIn('unit_price_micros', line)
                    self.assertEqual(line['formula'], f"round_half_up({line['units']} * {line['unit_price_micros']} / 10000) = {line['amount_minor']}")
            for line in trail['subscription_lines']:
                self.assertEqual(line['formula'], f"round_half_up({line['fee_minor']} * {line['days']} / {line['days_in_period']}) = {line['amount_minor']}")
                self.assertIn('from', line)
                self.assertIn('to', line)
        a_sources = run.audit['invoices']['acct_a']['usage_sources']['api_calls']
        self.assertEqual([(s['event_id'], s['line'], s['units']) for s in a_sources], [('a1', 1, 4000), ('a2', 2, 8000), ('a6', 7, 5)])
        self.assertEqual(run.audit['invoices']['acct_a']['quarantined_lines'], [8, 9])
        self.assertEqual(run.audit['invoices']['acct_a']['credit'], {'credit_minor': 500, 'applied_minor': 500, 'remaining_minor': 0})

    def test_reconcile_recomputes_every_invoice_from_the_trail(self):
        from rvn_ledger.audit import AuditError, reconcile
        run = run_fixture()
        reconcile(run.invoices, run.quarantine, run.audit, run.manifest)
        tampered = [
            ('invoice amount', lambda r: r.invoices[0]['lines'][1].__setitem__('amount_minor', 86001)),
            # subtotal unchanged: only the line-by-line comparison with the trail can see this one
            ('amount moved between lines', lambda r: (r.invoices[0]['lines'][1].__setitem__('amount_minor', 86001),
                                                      r.invoices[0]['lines'][2].__setitem__('amount_minor', 12230))),
            # sums unchanged: only the every-accepted-event-used-exactly-once check can see this one
            ('source reused across metrics', lambda r: r.audit['invoices']['acct_a']['usage_sources']['storage_gb_hours'].__setitem__(
                0, {'event_id': 'a1', 'line': 1, 'units': 100})),
            ('invoice total', lambda r: r.invoices[3].__setitem__('total_minor', 9650)),
            ('source units', lambda r: r.audit['invoices']['acct_a']['usage_sources']['api_calls'][0].__setitem__('units', 4001)),
            ('dropped source', lambda r: r.audit['invoices']['acct_a']['usage_sources']['api_calls'].pop()),
            ('tier price', lambda r: r.audit['invoices']['acct_b']['usage_lines'][0].__setitem__('unit_price_micros', 1901)),
            ('segment days', lambda r: r.audit['invoices']['acct_b']['subscription_lines'][0].__setitem__('days', 17)),
            ('credit', lambda r: r.invoices[2].__setitem__('credit_applied_minor', 27899)),
            ('quarantined count', lambda r: r.invoices[0].__setitem__('quarantined_count', 3)),
            ('quarantine entry', lambda r: r.quarantine.pop()),
            ('decision', lambda r: r.audit['decisions'][0].__setitem__('status', 'quarantined')),
            ('manifest count', lambda r: r.manifest['counts'].__setitem__('accepted', 1)),
            ('billable units', lambda r: r.invoices[1]['billable_units'].__setitem__('api_calls', 60001)),
            ('currency swap', lambda r: r.invoices[0].__setitem__('currency', 'USD')),
        ]
        for name, mutate in tampered:
            broken = copy.deepcopy(run)
            mutate(broken)
            with self.subTest(tamper=name), self.assertRaises(AuditError):
                reconcile(broken.invoices, broken.quarantine, broken.audit, broken.manifest)

    def test_quarantine_entries_are_in_source_order_with_primary_reason(self):
        run = run_fixture()
        self.assertEqual(run.quarantine, EXPECTED_QUARANTINE)
        self.assertEqual(run.invoices, EXPECTED_INVOICES)
        self.assertEqual(run.manifest['quarantine_reasons'],
                         {'invalid_json': 1, 'invalid_ts': 1, 'invalid_units': 1, 'nonpositive_units': 2, 'unknown_account': 1, 'unknown_metric': 1})
