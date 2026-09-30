import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from test_invoice import ACCOUNTS, EXPECTED_INVOICES, EXPECTED_QUARANTINE, PERIOD, PLANS, RAW_EVENTS

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / 'data'
CLI = ROOT / 'cli.py'


def write_fixture(directory: Path, accounts=ACCOUNTS, raw=RAW_EVENTS, period=PERIOD, plans=PLANS):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / 'events.jsonl').write_bytes(raw)
    (directory / 'accounts.json').write_text(json.dumps(accounts))
    (directory / 'plans.json').write_text(json.dumps(plans))
    (directory / 'period.json').write_text(json.dumps(period))
    return directory


def run_cli(*args, cwd=None, env=None):
    return subprocess.run([sys.executable, str(CLI), *map(str, args)], cwd=cwd, env=env, capture_output=True, text=True)


def snapshot(directory: Path) -> dict:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(directory.iterdir())}


class PipelineTests(unittest.TestCase):
    def test_run_ledger_reproduces_the_hand_verified_outputs(self):
        from rvn_ledger.pipeline import run_ledger
        raw = {'events.jsonl': RAW_EVENTS, 'accounts.json': json.dumps(ACCOUNTS).encode(), 'plans.json': json.dumps(PLANS).encode(),
               'period.json': json.dumps(PERIOD).encode()}
        run = run_ledger(raw)
        self.assertEqual(run.invoices, EXPECTED_INVOICES)
        self.assertEqual(run.quarantine, EXPECTED_QUARANTINE)
        self.assertEqual(run.manifest['counts'], {'raw': 18, 'accepted': 8, 'duplicate_ignored': 1, 'excluded_out_of_period': 1,
                                                  'excluded_late': 1, 'quarantined': 7, 'invoices': 4, 'quarantine_entries': 7,
                                                  'subscription_lines': 5, 'usage_lines': 8})
        self.assertEqual(run.manifest['inputs']['events.jsonl']['sha256'], hashlib.sha256(RAW_EVENTS).hexdigest())
        self.assertEqual(run.manifest['inputs']['events.jsonl']['records'], 18)
        self.assertEqual(run.manifest['inputs']['accounts.json']['records'], 4)
        self.assertEqual(run.manifest['totals_by_currency'], {'EUR': {'subtotal_minor': 27900, 'credit_applied_minor': 27900, 'total_minor': 0, 'invoices': 1},
                                                              'TRY': {'subtotal_minor': 198551, 'credit_applied_minor': 500, 'total_minor': 198051, 'invoices': 1},
                                                              'USD': {'subtotal_minor': 27718, 'credit_applied_minor': 1000, 'total_minor': 26718, 'invoices': 2}})
        self.assertIn('python', run.manifest['versions'])
        self.assertIn('code_sha256', run.manifest['versions'])

    def test_unusable_configuration_stops_before_any_output(self):
        from rvn_ledger.inputs import InputError
        from rvn_ledger.pipeline import run_ledger
        good = {'events.jsonl': RAW_EVENTS, 'accounts.json': json.dumps(ACCOUNTS).encode(), 'plans.json': json.dumps(PLANS).encode(),
                'period.json': json.dumps(PERIOD).encode()}
        for name, bad in [('accounts.json', b'{broken'), ('accounts.json', b'[]'), ('plans.json', b'[]'), ('period.json', b'{}'),
                          ('period.json', json.dumps({**PERIOD, 'days_in_period': 31}).encode()),
                          ('accounts.json', json.dumps([{**ACCOUNTS[0], 'timezone': 'localtime'}]).encode()),
                          ('events.jsonl', b'\xff\xfe')]:
            with self.subTest(name=name, bad=bad[:30]):
                if name == 'events.jsonl':
                    run = run_ledger({**good, name: bad})      # events are never fatal: the line is quarantined
                    self.assertEqual(run.quarantine, [{'event_id': None, 'reason': 'invalid_utf8'}])
                else:
                    with self.assertRaises(InputError):
                        run_ledger({**good, name: bad})

    def test_empty_event_stream_still_invoices_every_account(self):
        from rvn_ledger.pipeline import run_ledger
        run = run_ledger({'events.jsonl': b'', 'accounts.json': json.dumps(ACCOUNTS).encode(), 'plans.json': json.dumps(PLANS).encode(),
                          'period.json': json.dumps(PERIOD).encode()})
        self.assertEqual([inv['account_id'] for inv in run.invoices], ['acct_a', 'acct_b', 'acct_c', 'acct_d'])
        self.assertTrue(all(l['kind'] == 'subscription' for inv in run.invoices for l in inv['lines']))
        self.assertEqual(run.quarantine, [])
        self.assertEqual(run.manifest['counts']['raw'], 0)


class CliTests(unittest.TestCase):
    def test_run_writes_contract_outputs_and_is_byte_identical_across_runs_cwd_and_tz(self):
        with tempfile.TemporaryDirectory() as tmp:
            inputs = write_fixture(Path(tmp) / 'in')
            out1, out2, out3 = Path(tmp) / 'out1', Path(tmp) / 'out2', Path(tmp) / 'out3'
            first = run_cli('run', '--input-dir', inputs, '--out', out1)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(json.loads((out1 / 'invoices.json').read_bytes()), EXPECTED_INVOICES)
            self.assertEqual(json.loads((out1 / 'quarantine.json').read_bytes()), EXPECTED_QUARANTINE)
            manifest = json.loads((out1 / 'manifest.json').read_bytes())
            self.assertEqual(manifest['counts']['raw'], 18)
            second = run_cli('run', '--input-dir', inputs, '--out', out1)          # rerun into the same directory
            self.assertEqual(second.returncode, 0, second.stderr)
            other_cwd = run_cli('run', '--input-dir', inputs, '--out', out2, cwd=tmp)
            self.assertEqual(other_cwd.returncode, 0, other_cwd.stderr)
            env = {**os.environ, 'TZ': 'Pacific/Honolulu', 'PYTHONHASHSEED': '7'}
            other_tz = run_cli('run', '--events', inputs / 'events.jsonl', '--accounts', inputs / 'accounts.json',
                               '--plans', inputs / 'plans.json', '--period', inputs / 'period.json', '--out', out3, env=env)
            self.assertEqual(other_tz.returncode, 0, other_tz.stderr)
            self.assertEqual(snapshot(out1), snapshot(out2))
            self.assertEqual(snapshot(out1), snapshot(out3))
            self.assertEqual(sorted(snapshot(out1)), ['audit.json', 'invoices.json', 'manifest.json', 'quarantine.json'])
            check = run_cli('check', '--out', out1)
            self.assertEqual(check.returncode, 0, check.stderr)

    def test_exit_codes_distinguish_usage_configuration_and_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            inputs = write_fixture(Path(tmp) / 'in')
            out = Path(tmp) / 'out'
            self.assertEqual(run_cli().returncode, 2)
            self.assertEqual(run_cli('run', '--input-dir', str(Path(out) / 'missing-inputs'), '--out', out).returncode, 2)                                   # no inputs
            self.assertEqual(run_cli('run', '--input-dir', Path(tmp) / 'nowhere', '--out', out).returncode, 2)
            self.assertFalse(out.exists())
            broken = write_fixture(Path(tmp) / 'broken', period={**PERIOD, 'days_in_period': 31})
            result = run_cli('run', '--input-dir', broken, '--out', out)
            self.assertEqual(result.returncode, 2)
            self.assertIn('days_in_period', result.stderr)
            self.assertFalse((out / 'invoices.json').exists())                                            # nothing published
            self.assertEqual(run_cli('run', '--input-dir', inputs, '--out', out).returncode, 0)
            (out / 'invoices.json').write_bytes(b'[]\n')
            self.assertEqual(run_cli('check', '--out', out).returncode, 2)                                  # tampering detected

    def test_no_network_and_no_model_access_at_runtime(self):
        # Isolated Python still permits installed tzdata (required on Windows).
        # No model/network imports are present; -S would wrongly hide this data dependency.
        source = ''.join((ROOT / 'src' / 'rvn_ledger' / name).read_text() for name in ('cli.py', 'pipeline.py', 'invoice.py', 'tiers.py', 'audit.py', 'outputs.py',
                                                                'inputs.py', 'selection.py', 'validation.py', 'timing.py', 'aggregation.py',
                                                                'subscription.py', 'money.py'))
        for forbidden in ('import socket', 'import requests', 'urllib', 'http.client', 'anthropic', 'openai', 'API_KEY', 'subprocess'):
            self.assertNotIn(forbidden, source)
        with tempfile.TemporaryDirectory() as tmp:
            inputs = write_fixture(Path(tmp) / 'in')
            result = subprocess.run([sys.executable, '-I', str(CLI), 'run', '--input-dir', str(inputs), '--out', str(Path(tmp) / 'out')],
                                    capture_output=True, text=True, env={key: os.environ[key] for key in ('PATH', 'SystemRoot', 'WINDIR') if key in os.environ})
            self.assertEqual(result.returncode, 0, result.stderr)


@unittest.skipUnless(DATA.is_dir(), 'supplied inputs not present')
class RealDataTests(unittest.TestCase):
    """The supplied dataset; expected counts come from the timing/aggregation receipts (independent re-derivation)."""

    def test_supplied_inputs_run_twice_byte_identical_with_reconciled_counts(self):
        from rvn_ledger.audit import reconcile
        with tempfile.TemporaryDirectory() as tmp:
            out1, out2 = Path(tmp) / 'a', Path(tmp) / 'b'
            for out in (out1, out2):
                result = run_cli('run', '--input-dir', DATA, '--out', out)
                self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(snapshot(out1), snapshot(out2))
            invoices = json.loads((out1 / 'invoices.json').read_bytes())
            quarantine = json.loads((out1 / 'quarantine.json').read_bytes())
            manifest = json.loads((out1 / 'manifest.json').read_bytes())
            audit = json.loads((out1 / 'audit.json').read_bytes())
            self.assertEqual(manifest['counts']['raw'], 1078)
            self.assertEqual({k: manifest['counts'][k] for k in ('accepted', 'duplicate_ignored', 'excluded_out_of_period', 'excluded_late', 'quarantined')},
                             {'accepted': 1042, 'duplicate_ignored': 6, 'excluded_out_of_period': 8, 'excluded_late': 3, 'quarantined': 19})
            self.assertEqual(len(invoices), 41)
            self.assertEqual([inv['account_id'] for inv in invoices], sorted(inv['account_id'] for inv in invoices))
            self.assertEqual(len(quarantine), 19)
            self.assertEqual(sum(inv['billable_units']['api_calls'] for inv in invoices), 3552628)
            self.assertEqual(sum(inv['billable_units']['storage_gb_hours'] for inv in invoices), 50267)
            self.assertEqual(sum(l['amount_minor'] for inv in invoices for l in inv['lines'] if l['kind'] == 'subscription' and inv['currency'] == 'USD'), 231800)
            self.assertEqual(sum(l['amount_minor'] for inv in invoices for l in inv['lines'] if l['kind'] == 'subscription' and inv['currency'] == 'EUR'), 145733)
            self.assertEqual(sum(l['amount_minor'] for inv in invoices for l in inv['lines'] if l['kind'] == 'subscription' and inv['currency'] == 'TRY'), 7901167)
            for inv in invoices:
                self.assertEqual(inv['subtotal_minor'], sum(l['amount_minor'] for l in inv['lines']))
                self.assertEqual(inv['total_minor'], inv['subtotal_minor'] - inv['credit_applied_minor'])
                self.assertGreaterEqual(inv['total_minor'], 0)
            reconcile(invoices, quarantine, audit, manifest)
            self.assertEqual(run_cli('check', '--out', out1).returncode, 0)
