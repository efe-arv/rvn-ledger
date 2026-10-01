"""Final-review boundaries: supported integers, ZIP input, and prepared billing."""
import copy
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
import zipfile

from rvn_ledger.context import prepare_context
from rvn_ledger.inputs import InputError, MAX_INTEGER_DIGITS, read_events, read_json
from rvn_ledger.pipeline import run_ledger
from test_invoice import ACCOUNTS, PERIOD, PLANS, RAW_EVENTS, EXPECTED_INVOICES, event
from test_pipeline import run_cli, write_fixture


class IntegerBoundaryTests(unittest.TestCase):
    def test_admission_boundary_and_identity_recovery(self):
        for sign in ('', '-'):
            for digits in (MAX_INTEGER_DIGITS - 1, MAX_INTEGER_DIGITS, MAX_INTEGER_DIGITS + 1, 4300):
                raw = ('{"event_id":"oversized","ingest_seq":1,"units":' + sign + '9' * digits + '}').encode()
                row = read_events(raw)[0]
                with self.subTest(sign=sign, digits=digits):
                    if digits <= MAX_INTEGER_DIGITS:
                        self.assertIsNone(row.error)
                        self.assertEqual(row.value['units'], int(sign + '9' * digits))
                    else:
                        self.assertEqual(row.error, 'integer_too_large')
                        self.assertEqual(row.salvaged_identity.event_id, 'oversized')
                        with self.assertRaises(InputError):
                            read_json(raw, 'plans.json')

    def test_original_4300_digit_reproducer_keeps_billing_other_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lines = []
            for i in range(2):
                text = json.dumps(event(f'huge-{i}', i, 'acct_a', units=0))
                lines.append(text.replace('"units": 0', '"units": ' + '9' * 4300).encode())
            raw = RAW_EVENTS + b'\n'.join(lines) + b'\n'
            inputs = write_fixture(root / 'in', raw=raw)
            out = root / 'out'
            result = run_cli('run', '--input-dir', inputs, '--out', out, '--json')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads((out / 'invoices.json').read_bytes()), EXPECTED_INVOICES)
            quarantined = json.loads((out / 'quarantine.json').read_bytes())
            self.assertEqual(quarantined[-2:], [{'event_id': f'huge-{i}', 'reason': 'integer_too_large'} for i in range(2)])
            self.assertEqual(run_cli('check', '--out', out).returncode, 0)

    def test_rejected_canonical_integer_cannot_be_replaced(self):
        from rvn_ledger.selection import deduplicate
        prefix = b'{"event_id":"same","ingest_seq":1,"units":'
        rows = read_events(prefix + b'9' * (MAX_INTEGER_DIGITS + 1) + b'}\n'
                           + b'{"event_id":"same","ingest_seq":2,"units":1}\n')
        result = deduplicate(rows)
        self.assertEqual(result.rejected, {1: 'integer_too_large'})
        self.assertEqual(result.duplicates, {2: 1})

    def test_maximum_admitted_products_serialize_under_minimum_python_limit(self):
        largest = 10**MAX_INTEGER_DIGITS - 1
        plans = copy.deepcopy(PLANS)
        for plan in plans.values():
            for prices in plan['prices'].values():
                prices['subscription_fee_minor'] = largest
                for tiers in prices['metrics'].values():
                    for tier in tiers:
                        tier['unit_price_micros'] = largest
        values = [event(f'boundary-{i}', i, 'acct_a', units=largest) for i in range(3)]
        raw = ('\n'.join(json.dumps(v) for v in values) + '\n').encode()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inputs = write_fixture(root / 'in', raw=raw, plans=plans)
            out = root / 'out'
            env = {**os.environ, 'PYTHONINTMAXSTRDIGITS': '640'}
            result = run_cli('run', '--input-dir', inputs, '--out', out, env=env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(run_cli('check', '--out', out, env=env).returncode, 0)
            invoices = json.loads((out / 'invoices.json').read_bytes())
            self.assertEqual(invoices[0]['billable_units']['api_calls'], largest * 3)


class ArchiveBoundaryTests(unittest.TestCase):
    def test_unsupported_zip_flags_and_compression_return_input_error(self):
        from rvn_ledger.excel import export_inputs
        raw = {'events.jsonl': RAW_EVENTS, 'accounts.json': json.dumps(ACCOUNTS).encode(),
               'period.json': json.dumps(PERIOD).encode(), 'plans.json': json.dumps(PLANS).encode()}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = root / 'original.xlsx'
            export_inputs(raw, original)
            for name, flag, compression in [('encrypted', 1, None), ('strong', 64, None),
                                             ('patched', 32, None), ('compression', 0, 99)]:
                content = bytearray(original.read_bytes())
                with zipfile.ZipFile(original) as archive:
                    infos, central = archive.infolist(), archive.start_dir
                for info in infos:
                    struct.pack_into('<H', content, info.header_offset + 6, info.flag_bits | flag)
                    if compression is not None:
                        struct.pack_into('<H', content, info.header_offset + 8, compression)
                for info in infos:
                    struct.pack_into('<H', content, central + 8, info.flag_bits | flag)
                    if compression is not None:
                        struct.pack_into('<H', content, central + 10, compression)
                    central += 46 + sum(struct.unpack_from('<HHH', content, central + 28))
                path, target = root / f'{name}.xlsx', root / name
                path.write_bytes(content)
                result = run_cli('import', '--file', path, '--input-dir', target)
                with self.subTest(name=name):
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertNotIn('Traceback', result.stderr)
                    self.assertFalse(target.exists())


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
