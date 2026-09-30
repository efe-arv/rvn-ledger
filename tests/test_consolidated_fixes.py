"""Regressions for Efe and Astra's consolidated review findings."""
import copy
import importlib.util
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from test_audit import run_fixture
from test_invoice import ACCOUNTS, PERIOD, PLANS, RAW_EVENTS
from test_pipeline import ROOT, CLI, write_fixture, run_cli
import test_timing


class ConsolidatedFixTests(unittest.TestCase):
    def test_surrogate_record_quarantines_and_valid_records_still_publish(self):
        for field in ('event_id', 'metric', 'extra', 'nested'):
            event = {'event_id': 'bad', 'ingest_seq': 0, 'account_id': ACCOUNTS[0]['account_id'],
                     'metric': 'api_calls', 'units': None, 'ts': '2026-09-15T12:00:00Z',
                     'ingested_at': '2026-09-15T12:00:00Z'}
            event[field] = {'key\ud800': ['\udfff']} if field == 'nested' else 'bad\ud800'
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                inputs = write_fixture(Path(tmp) / 'in', raw=json.dumps(event).encode() + b'\n' + RAW_EVENTS)
                out = Path(tmp) / 'out'
                result = run_cli('run', '--input-dir', inputs, '--out', out)
                self.assertEqual(result.returncode, 0, result.stderr)
                quarantine = json.loads((out / 'quarantine.json').read_bytes())
                self.assertEqual(quarantine[0]['reason'], 'invalid_unicode')
                self.assertEqual(run_cli('check', '--out', out).returncode, 0)
                self.assertEqual(json.loads((out / 'manifest.json').read_bytes())['counts']['accepted'], 8)

    def test_serialization_preserves_invalid_identity_without_utf8_crash(self):
        from rvn_ledger.outputs import serialize
        document = {'id': '\ud800', 'normal': 'ş😀', '\udfff': ['\ud800']}
        encoded = serialize(document)
        self.assertEqual(json.loads(encoded.decode('utf-8')), document)
        self.assertIn('ş😀'.encode(), encoded)

    def test_configuration_surrogates_fail_closed(self):
        from rvn_ledger.inputs import InputError, read_json
        with self.assertRaises(InputError):
            read_json(b'{"x":"\\ud800"}', 'config.json')

    def test_submicrosecond_cutoff_and_period_boundaries(self):
        classify = test_timing.TimingTests().classify
        for offset, cutoff in [('Z', '2026-10-03T00:00:00'), ('+03:00', '2026-10-03T03:00:00')]:
            self.assertEqual(classify('2026-09-15T12:00:00Z', cutoff + '.0000000' + offset), 'accepted')
            for digits in ('0000001', '000000000000000000001'):
                self.assertEqual(classify('2026-09-15T12:00:00Z', cutoff + '.' + digits + offset), 'excluded_late')
        self.assertEqual(classify('2026-09-30T23:59:59.999999999Z'), 'accepted')
        self.assertEqual(classify('2026-10-01T00:00:00.000000001Z'), 'excluded_out_of_period')
        self.assertEqual(classify('2026-08-31T23:59:59.999999999Z'), 'excluded_out_of_period')

    def test_shifted_tier_units_rejected_even_with_consistent_amounts(self):
        from rvn_ledger.audit import AuditError, reconcile
        run = run_fixture()
        inv = run.invoices[0]
        tiers = [u for u in run.audit['invoices'][inv['account_id']]['usage_lines'] if u['metric'] == 'api_calls']
        self.assertGreaterEqual(len(tiers), 2)
        old_subtotal = inv['subtotal_minor']
        for u, delta in zip(tiers[:2], (-1000, 1000)):
            u['units'] += delta
            u['amount_minor'] = (2 * u['units'] * u['unit_price_micros'] + 10000) // 20000
            u['formula'] = f"round_half_up({u['units']} * {u['unit_price_micros']} / 10000) = {u['amount_minor']}"
            line = next(l for l in inv['lines'] if l.get('metric') == 'api_calls' and l.get('tier_from') == u['tier_from'])
            line.update(units=u['units'], amount_minor=u['amount_minor'])
        inv['subtotal_minor'] = sum(l['amount_minor'] for l in inv['lines'])
        inv['total_minor'] = inv['subtotal_minor'] - inv['credit_applied_minor']
        bucket = run.manifest['totals_by_currency'][inv['currency']]
        bucket['subtotal_minor'] += inv['subtotal_minor'] - old_subtotal
        bucket['total_minor'] += inv['subtotal_minor'] - old_subtotal
        with self.assertRaises(AuditError):
            reconcile(run.invoices, run.quarantine, run.audit, run.manifest)
        # Re-hash the forged documents: check must reject at reconciliation, not merely hashes.
        from rvn_ledger.outputs import publish, serialize
        with tempfile.TemporaryDirectory() as tmp:
            publish(tmp, {n: serialize(d) for n, d in [('invoices.json', run.invoices),
                    ('quarantine.json', run.quarantine), ('audit.json', run.audit)]}, run.manifest)
            result = run_cli('check', '--out', tmp)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertNotIn('Traceback', result.stderr)

    def test_duplicate_target_and_formula_corruption_rejected(self):
        from rvn_ledger.audit import AuditError, reconcile
        for mutate in (lambda r: r.audit['decisions'][2].update(canonical_line=999999),
                       lambda r: r.audit['invoices']['acct_a']['subscription_lines'][0].update(formula='wrong')):
            run = run_fixture()
            mutate(run)
            with self.assertRaises(AuditError):
                reconcile(run.invoices, run.quarantine, run.audit, run.manifest)

    def test_malformed_output_metadata_is_controlled_error(self):
        from rvn_ledger.outputs import OutputError, check_outputs, publish
        for bad in (None, [], 'bad', 3, {}, {'sha256': 'x', 'bytes': True}):
            with self.subTest(bad=bad), tempfile.TemporaryDirectory() as tmp:
                publish(tmp, {'invoices.json': b'[]', 'quarantine.json': b'[]', 'audit.json': b'{}'}, {})
                path = Path(tmp) / 'manifest.json'
                manifest = json.loads(path.read_bytes())
                manifest['outputs']['invoices.json'] = bad
                path.write_text(json.dumps(manifest))
                with self.assertRaises(OutputError):
                    check_outputs(tmp)
                result = run_cli('check', '--out', tmp)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertNotIn('Traceback', result.stderr)

    def test_manifest_nested_container_types_are_validated(self):
        from rvn_ledger.outputs import publish, serialize
        for field in ('counts', 'quarantine_reasons', 'totals_by_currency', 'inputs', 'rules', 'versions'):
            run = run_fixture()
            run.manifest[field] = None
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                publish(tmp, {'invoices.json': serialize(run.invoices), 'quarantine.json': serialize(run.quarantine),
                              'audit.json': serialize(run.audit)}, run.manifest)
                result = run_cli('check', '--out', tmp)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertNotIn('Traceback', result.stderr)

    def test_bad_nested_audit_is_controlled_cli_error(self):
        from rvn_ledger.outputs import publish, serialize
        for field in ('decisions', 'invoices'):
            run = run_fixture()
            run.audit[field] = None
            with tempfile.TemporaryDirectory() as tmp:
                publish(tmp, {'invoices.json': serialize(run.invoices), 'quarantine.json': serialize(run.quarantine),
                              'audit.json': serialize(run.audit)}, run.manifest)
                result = run_cli('check', '--out', tmp)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertNotIn('Traceback', result.stderr)

    def test_every_replacement_failure_restores_previous_good_output(self):
        from rvn_ledger.outputs import check_outputs, publish
        files = {'invoices.json': b'[]', 'quarantine.json': b'[]', 'audit.json': b'{}'}
        for fail_at in range(1, 5):
            with self.subTest(fail_at=fail_at), tempfile.TemporaryDirectory() as tmp:
                publish(tmp, files, {'counts': {'raw': 0}})
                before = {p.name: p.read_bytes() for p in Path(tmp).iterdir()}
                real_replace = os.replace
                calls = 0
                def fail_once(src, dst):
                    nonlocal calls
                    calls += 1
                    if calls == fail_at:
                        raise OSError('injected replace failure')
                    return real_replace(src, dst)
                with patch('rvn_ledger.outputs.os.replace', side_effect=fail_once), self.assertRaises(OSError):
                    publish(tmp, {n: b'[1]' for n in files}, {'counts': {'raw': 1}})
                self.assertEqual({p.name: p.read_bytes() for p in Path(tmp).iterdir()}, before)
                check_outputs(tmp)

    def test_actual_manifest_has_no_system_path(self):
        run = run_fixture()
        self.assertNotIn('/usr/', json.dumps(run.manifest))
        self.assertNotIn(str(ROOT), json.dumps(run.manifest))

    def test_configuration_is_checked_before_event_reader(self):
        from rvn_ledger.inputs import InputError
        from rvn_ledger.pipeline import run_ledger
        broken = copy.deepcopy(ACCOUNTS)
        broken[0]['credit_minor'] = -1
        raw = {'events.jsonl': RAW_EVENTS, 'accounts.json': json.dumps(broken).encode(),
               'plans.json': json.dumps(PLANS).encode(), 'period.json': json.dumps(PERIOD).encode()}
        with patch('rvn_ledger.pipeline.read_events', side_effect=AssertionError('events read before config')):
            with self.assertRaises(InputError):
                run_ledger(raw)

    def test_oracle_keeps_submicrosecond_precision_and_unicode_policy(self):
        import test_selection
        from verify_timing import independent_statuses
        fixture = test_selection.ClassificationTests()
        events = [fixture.event(event_id='late', ingested_at='2026-10-02T21:00:00.0000001Z'),
                  fixture.event(event_id='invalid', extra='\ud800')]
        raw = ('\n'.join(json.dumps(event) for event in events) + '\n').encode()
        self.assertEqual(independent_statuses(raw, fixture.accounts, fixture.period),
                         {1: 'excluded_late', 2: 'quarantined'})

    def test_staging_directory_blocks_publish_and_check_without_deletion(self):
        from rvn_ledger.outputs import OutputError, publish, check_outputs
        with tempfile.TemporaryDirectory() as tmp:
            staging = Path(tmp) / '.ledger-staging'
            staging.mkdir()
            (staging / 'recovery').write_bytes(b'keep')
            with self.assertRaises(OutputError):
                publish(tmp, {'invoices.json': b'[]', 'quarantine.json': b'[]', 'audit.json': b'{}'}, {})
            with self.assertRaises(OutputError):
                check_outputs(tmp)
            self.assertEqual((staging / 'recovery').read_bytes(), b'keep')

    def test_rollback_failure_keeps_recovery_and_fails_closed(self):
        from rvn_ledger.outputs import OutputError, publish, check_outputs
        with tempfile.TemporaryDirectory() as tmp:
            files = {'invoices.json': b'[]', 'quarantine.json': b'[]', 'audit.json': b'{}'}
            publish(tmp, files, {})
            actual = os.replace
            calls = 0
            def persistent_failure(src, dst):
                nonlocal calls
                calls += 1
                if calls >= 2:
                    raise OSError('persistent storage failure')
                return actual(src, dst)
            with patch('rvn_ledger.outputs.os.replace', side_effect=persistent_failure), self.assertRaises(OutputError):
                publish(tmp, {n: b'[1]' for n in files}, {})
            self.assertTrue((Path(tmp) / '.ledger-staging' / 'previous' / 'invoices.json').exists())
            with self.assertRaises(OutputError):
                check_outputs(tmp)

    def test_io_error_has_controlled_cli_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            inputs = write_fixture(Path(tmp) / 'in')
            out = Path(tmp) / 'file-not-directory'
            out.write_bytes(b'keep')
            result = run_cli('run', '--input-dir', inputs, '--out', out)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertNotIn('Traceback', result.stderr)
            self.assertEqual(out.read_bytes(), b'keep')

    @unittest.skipUnless(importlib.util.find_spec("tzdata"), "optional tzdata package absent; system timezone path tested elsewhere")
    def test_isolated_runtime_with_package_only_timezone_database(self):
        # Simulates Windows' absent system zoneinfo tree without claiming Windows execution.
        with tempfile.TemporaryDirectory() as tmp:
            inputs = write_fixture(Path(tmp) / 'in')
            script = "import zoneinfo,runpy,sys; zoneinfo.reset_tzpath([]); sys.argv=sys.argv[1:]; runpy.run_path(sys.argv[0],run_name='__main__')"
            result = subprocess.run([sys.executable, '-I', '-c', script, str(CLI), 'run', '--input-dir', str(inputs),
                                     '--out', str(Path(tmp) / 'out')], capture_output=True, text=True,
                                    env={**os.environ, 'PYTHONTZPATH': ''})
            self.assertEqual(result.returncode, 0, result.stderr)
