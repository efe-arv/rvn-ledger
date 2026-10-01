"""Invariants that must hold for any input, checked on seeded generated inputs.

- total = subtotal - credit applied, never negative; subtotal = sum of rounded lines
- credit applied + credit remaining = credit; more credit never raises a total
- every raw line gets exactly one disposition; accepted units = billable units = tier units
- a later duplicate, the file order, or another account's events never change an invoice
- the same inputs serialize to the same bytes
"""
import json
import random
import unittest
from collections import Counter

from rvn_ledger.audit import reconcile
from rvn_ledger.outputs import serialize
from rvn_ledger.pipeline import run_ledger

from fixtures import ACCOUNTS, build, event, PERIOD, PLANS, raw_lines


class InvoicePropertyTests(unittest.TestCase):
    """Generated valid inputs, fixed seed; any failure can be reduced to the hand-verified fixture."""
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


class MessyRunInvariantTests(unittest.TestCase):
    """Whole runs over generated messy streams: duplicates, junk, late and out-of-period events."""
    rng = random.Random(20261001)

    def messy_stream(self, n=120):
        rng, values = self.rng, []
        accounts = [a['account_id'] for a in ACCOUNTS] + ['acct_unknown']
        for i in range(n):
            kind = rng.random()
            units = rng.choice([rng.randrange(1, 40000), 0, -3, '7', 2.5]) if kind < 0.15 else rng.randrange(1, 40000)
            day = rng.randrange(1, 31)
            ts = f'2026-{"08-31" if kind > 0.95 else f"09-{day:02d}"}T{rng.randrange(0, 24):02d}:{rng.randrange(0, 60):02d}:00Z'
            ingested = '2026-10-09T00:00:00Z' if 0.9 < kind <= 0.95 else ts
            values.append(event(f'm{rng.randrange(0, n // 2)}', rng.randrange(1, 10 * n), rng.choice(accounts),
                                rng.choice(['api_calls', 'storage_gb_hours', 'gpu']), units, ts=ts, ingested_at=ingested))
        return values

    def run_stream(self, values, accounts=ACCOUNTS):
        return run_ledger({'events.jsonl': raw_lines(values), 'accounts.json': json.dumps(accounts).encode(),
                           'plans.json': json.dumps(PLANS).encode(), 'period.json': json.dumps(PERIOD).encode()})

    def test_every_run_reconciles_and_keeps_the_money_invariants(self):
        for _ in range(15):
            values = self.messy_stream()
            result = self.run_stream(values)
            reconcile(result.invoices, result.quarantine, result.audit, result.manifest)
            credits = {a['account_id']: a['credit_minor'] for a in ACCOUNTS}
            for inv in result.invoices:
                with self.subTest(account=inv['account_id']):
                    self.assertEqual(inv['subtotal_minor'], sum(l['amount_minor'] for l in inv['lines']))
                    self.assertEqual(inv['total_minor'], inv['subtotal_minor'] - inv['credit_applied_minor'])
                    self.assertGreaterEqual(inv['total_minor'], 0)
                    self.assertEqual(inv['credit_applied_minor'] + inv['credit_remaining_minor'], credits[inv['account_id']])
                    for metric, units in inv['billable_units'].items():
                        self.assertEqual(units, sum(l['units'] for l in inv['lines'] if l.get('metric') == metric))

    def test_every_line_has_one_disposition_and_accepted_units_are_conserved(self):
        for _ in range(15):
            values = self.messy_stream()
            result = self.run_stream(values)
            decisions = result.audit['decisions']
            self.assertEqual([d['line'] for d in decisions], list(range(1, len(values) + 1)))
            counts = Counter(d['status'] for d in decisions)
            self.assertEqual(sum(counts.values()), len(values))
            accepted = sum(values[d['line'] - 1]['units'] for d in decisions if d['status'] == 'accepted')
            billed = sum(units for inv in result.invoices for units in inv['billable_units'].values())
            self.assertEqual(accepted, billed)

    def test_same_inputs_serialize_to_the_same_bytes(self):
        values = self.messy_stream()
        first, second = self.run_stream(values), self.run_stream(list(values))
        for name in ('invoices', 'quarantine', 'audit', 'manifest'):
            self.assertEqual(serialize(getattr(first, name)), serialize(getattr(second, name)), name)
