"""The prepared billing context: resolved once, read-only."""
import copy
import json
import unittest

from rvn_ledger.context import prepare_context
from rvn_ledger.pipeline import run_ledger

from fixtures import ACCOUNTS, EXPECTED_INVOICES, PERIOD, PLANS, RAW_EVENTS


class PreparedContextTests(unittest.TestCase):
    def test_context_has_no_mutable_configuration_aliases(self):
        accounts, plans = copy.deepcopy(ACCOUNTS), copy.deepcopy(PLANS)
        context = prepare_context(accounts, PERIOD, plans)
        first = context.accounts['acct_a']
        credit = first.credit_minor
        rate = first.tariffs['api_calls'][0].unit_price_micros
        accounts[0]['credit_minor'] = 123456
        plans['starter']['prices']['TRY']['metrics']['api_calls'][0]['unit_price_micros'] = 1
        self.assertEqual(first.credit_minor, credit)
        self.assertEqual(first.tariffs['api_calls'][0].unit_price_micros, rate)
        with self.assertRaises(TypeError):
            context.accounts['new'] = first
        with self.assertRaises(TypeError):
            first.tariffs['new'] = ()

    def test_context_resolution_and_aggregation_happen_once_per_run(self):
        from unittest.mock import patch
        import rvn_ledger.context as context
        import rvn_ledger.pipeline as pipeline
        raw = {'events.jsonl': RAW_EVENTS, 'accounts.json': json.dumps(ACCOUNTS).encode(),
               'period.json': json.dumps(PERIOD).encode(), 'plans.json': json.dumps(PLANS).encode()}
        with patch.object(context, 'account_bounds', wraps=context.account_bounds) as bounds, \
             patch.object(context, 'all_subscriptions', wraps=context.all_subscriptions) as subscriptions, \
             patch.object(pipeline, 'aggregate_usage', wraps=pipeline.aggregate_usage) as aggregate:
            result = run_ledger(raw)
        self.assertEqual(result.invoices, EXPECTED_INVOICES)
        self.assertEqual((bounds.call_count, subscriptions.call_count, aggregate.call_count), (1, 1, 1))
