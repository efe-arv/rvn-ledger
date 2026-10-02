import copy
import hashlib
import importlib.util
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fixtures import (ACCOUNTS, CLI_ARGS, DATA, EXPECTED_INVOICES, EXPECTED_QUARANTINE, PERIOD, PLANS, RAW_EVENTS, ROOT, SCRATCH, run_cli,
                      run_fixture, snapshot, write_fixture)


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
            result = subprocess.run([sys.executable, '-I', *CLI_ARGS[1:], 'run', '--input-dir', str(inputs), '--out', str(Path(tmp) / 'out')],
                                    capture_output=True, text=True, env={key: os.environ[key] for key in ('PATH', 'SystemRoot', 'WINDIR') if key in os.environ})
            self.assertEqual(result.returncode, 0, result.stderr)

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
            script = "import zoneinfo,runpy,sys; zoneinfo.reset_tzpath([]); sys.argv=['rvn-ledger']+sys.argv[1:]; runpy.run_module('rvn_ledger',run_name='__main__')"
            result = subprocess.run([sys.executable, '-I', '-c', script, 'run', '--input-dir', str(inputs),
                                     '--out', str(Path(tmp) / 'out')], capture_output=True, text=True,
                                    env={**os.environ, 'PYTHONTZPATH': ''})
            self.assertEqual(result.returncode, 0, result.stderr)


@unittest.skipUnless(DATA.is_dir(), 'supplied inputs not present')
class RealDataTests(unittest.TestCase):
    """The real input set in ./data, when present (it is not committed); counts were cross-checked by an independent re-derivation."""

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


class TimezoneProvenanceTests(unittest.TestCase):
    """External review (2026-10-02) F3: the manifest must name the TZif data each billed zone actually
    resolved against, per zone, instead of assuming the database that holds the first UTC file on TZPATH."""

    @staticmethod
    def expected_source(key):
        # Independent re-derivation of zoneinfo's lookup order: a TZPATH file first, else the tzdata package.
        import zoneinfo
        for root in zoneinfo.TZPATH:
            path = Path(root) / key
            if path.is_file():
                return 'system', hashlib.sha256(path.read_bytes()).hexdigest()
        from importlib import resources
        package, _, name = ('tzdata.zoneinfo.' + key.replace('/', '.')).rpartition('.')
        return 'tzdata package', hashlib.sha256(resources.files(package).joinpath(name).read_bytes()).hexdigest()

    def test_manifest_records_source_and_hash_of_every_billed_zone(self):
        run = run_fixture()
        versions = run.manifest['versions']
        self.assertEqual(list(versions), ['python', 'implementation', 'tzdata', 'timezones', 'code_sha256'])
        zones = versions['timezones']
        self.assertEqual(list(zones), sorted({a['timezone'] for a in ACCOUNTS}))
        summaries = set()
        for key, record in zones.items():
            with self.subTest(zone=key):
                self.assertEqual(set(record), {'source', 'version', 'sha256'})
                self.assertEqual((record['source'], record['sha256']), self.expected_source(key))
                self.assertIsInstance(record['version'], str)
                summaries.add(f"{record['source']} {record['version']}")
        # The one-line summary stays as before when every zone came from one database, and says so otherwise.
        self.assertEqual(versions['tzdata'], summaries.pop() if len(summaries) == 1 else 'mixed: ' + ', '.join(sorted(summaries)))

    @unittest.skipUnless(importlib.util.find_spec('tzdata'), 'optional tzdata package absent; cannot build a mixed-source run')
    def test_tzpath_file_shadowing_a_zone_is_recorded_as_that_zone_s_actual_source(self):
        # The review's repro: a TZPATH tree holding only Europe/Istanbul, filled with UTC bytes. zoneinfo bills
        # Istanbul accounts on those bytes, so the manifest must say so instead of naming the package for all zones.
        from importlib import resources
        utc_bytes = resources.files('tzdata.zoneinfo').joinpath('UTC').read_bytes()
        with tempfile.TemporaryDirectory(dir=SCRATCH) as tmp:
            tree = Path(tmp) / 'tz'
            (tree / 'Europe').mkdir(parents=True)
            (tree / 'Europe' / 'Istanbul').write_bytes(utc_bytes)
            inputs = write_fixture(Path(tmp) / 'in')
            out = Path(tmp) / 'out'
            result = run_cli('run', '--input-dir', inputs, '--out', out, env={**os.environ, 'PYTHONTZPATH': str(tree)})
            self.assertEqual(result.returncode, 0, result.stderr)
            manifest = json.loads((out / 'manifest.json').read_bytes())
            zones = manifest['versions']['timezones']
            self.assertEqual(zones['Europe/Istanbul'],
                             {'source': 'system', 'version': '(version file absent)', 'sha256': hashlib.sha256(utc_bytes).hexdigest()})
            for key in ('America/New_York', 'Asia/Tokyo', 'UTC'):
                self.assertEqual(zones[key]['source'], 'tzdata package', key)
            self.assertTrue(manifest['versions']['tzdata'].startswith('mixed: '), manifest['versions']['tzdata'])
            # The billing really did follow the shadowing file: with Istanbul at UTC the one-second-late event is in time.
            self.assertEqual(manifest['counts']['excluded_late'], 0)
            self.assertNotEqual(json.loads((out / 'invoices.json').read_bytes()), EXPECTED_INVOICES)
            self.assertEqual(run_cli('check', '--out', out).returncode, 0)

    def test_tzdata_version_summarises_only_the_zones_it_is_given(self):
        from rvn_ledger.pipeline import tzdata_version
        from rvn_ledger.timing import timezone_provenance
        provenance = timezone_provenance(['UTC', 'Europe/Istanbul', 'UTC'])
        self.assertEqual(list(provenance), ['Europe/Istanbul', 'UTC'])
        self.assertEqual(tzdata_version(['UTC']), f"{provenance['UTC']['source']} {provenance['UTC']['version']}")
        with self.assertRaises(TypeError):
            tzdata_version()
        # Only usable IANA keys are resolved: a host-dependent key or a path-shaped key is never read, let alone hashed.
        for key in ('localtime', 'Factory', '../../../etc/hostname', '/etc/hostname', 'Mars/Olympus', ''):
            with self.subTest(key=key):
                self.assertEqual(timezone_provenance([key]), {key: {'source': 'unknown', 'version': '', 'sha256': None}})
        self.assertEqual(tzdata_version(['/etc/hostname']), 'unknown')


class LeadingByteOrderMarkRunTests(unittest.TestCase):
    """The demo's first line (`api-first`, seq 2) has a conflicting later copy naming another account.
    A UTF-8 BOM in front of that line must not cost it its identity: otherwise the copy becomes canonical
    and bills usage the hand-derived outputs say does not exist. Hashes stay those of the raw bytes."""

    BOM = b'\xef\xbb\xbf'

    def test_demo_with_a_leading_bom_bills_exactly_the_hand_derived_outputs(self):
        demo = ROOT / 'examples' / 'demo'
        raw = (demo / 'events.jsonl').read_bytes()
        with tempfile.TemporaryDirectory(dir=SCRATCH) as tmp:
            inputs = Path(tmp) / 'in'
            inputs.mkdir()
            for name in ('accounts.json', 'plans.json', 'period.json'):
                shutil.copyfile(demo / name, inputs / name)
            (inputs / 'events.jsonl').write_bytes(self.BOM + raw)
            out = Path(tmp) / 'out'
            result = run_cli('run', '--input-dir', inputs, '--out', out)
            self.assertEqual(result.returncode, 0, result.stderr)
            for name in ('invoices', 'quarantine'):
                self.assertEqual(json.loads((out / f'{name}.json').read_bytes()), json.loads((demo / f'expected-{name}.json').read_bytes()), name)
            decisions = json.loads((out / 'audit.json').read_bytes())['decisions']
            first_line = raw.split(b'\n', 1)[0] + b'\n'
            self.assertEqual({k: decisions[0][k] for k in ('line', 'event_id', 'status')}, {'line': 1, 'event_id': 'api-first', 'status': 'accepted'})
            self.assertEqual(decisions[0]['sha256'], hashlib.sha256(self.BOM + first_line).hexdigest())
            self.assertEqual({k: decisions[1][k] for k in ('event_id', 'status', 'canonical_line')}, {'event_id': 'api-first', 'status': 'duplicate_ignored', 'canonical_line': 1})
            manifest = json.loads((out / 'manifest.json').read_bytes())
            self.assertEqual(manifest['inputs']['events.jsonl'], {'sha256': hashlib.sha256(self.BOM + raw).hexdigest(), 'bytes': len(raw) + 3, 'records': 9})
            self.assertEqual(run_cli('check', '--out', out).returncode, 0)
            copies = run_cli('explain', '--out', out, '--event-id', 'api-first', '--events', inputs / 'events.jsonl', '--json')
            self.assertEqual(copies.returncode, 0, copies.stderr)
            payload = json.loads(copies.stdout)
            self.assertTrue(payload['source_values_verified'])
            self.assertEqual([(r['source']['line'], r['status']) for r in payload['records']], [(1, 'accepted'), (2, 'duplicate_ignored')])
            self.assertEqual(payload['records'][1]['canonical_line'], 1)


class ZoneRulesMatchRecordedBytesTests(unittest.TestCase):
    """The provenance record must describe the bytes the run was billed on. `ZoneInfo(key)` serves a
    process-wide cache that ignores later TZPATH changes and later file changes, while a hash taken
    by re-reading the file describes whatever is on disk at that moment; the two can disagree. Each
    zone is therefore built from one read of its TZif bytes and the record is taken from that same read."""

    @staticmethod
    def fixed_offset_tzif(seconds: int, abbreviation: bytes = b'TST') -> bytes:
        # A minimal version-1 TZif file with no transitions and one local time type.
        abbreviation += b'\0'
        return (b'TZif' + b'\x00' + b'\x00' * 15 + struct.pack('>6l', 0, 0, 0, 0, 1, len(abbreviation))
                + struct.pack('>lBB', seconds, 0, 0) + abbreviation)

    @unittest.skipUnless(importlib.util.find_spec('tzdata'), 'optional tzdata package absent; the other fixture zones need it')
    def test_run_bills_on_the_bytes_it_records_even_when_zoneinfo_has_the_zone_cached(self):
        import zoneinfo
        from zoneinfo import ZoneInfo
        from rvn_ledger.timing import account_bounds
        custom = self.fixed_offset_tzif(5 * 3600)
        cached = ZoneInfo('Europe/Istanbul')        # whatever this host had: now in the process cache
        self.assertEqual(datetime(2026, 9, 1, tzinfo=cached).utcoffset(), timedelta(hours=3))
        original = zoneinfo.TZPATH
        with tempfile.TemporaryDirectory(dir=SCRATCH) as tmp:
            tree = Path(tmp) / 'tz'
            (tree / 'Europe').mkdir(parents=True)
            (tree / 'Europe' / 'Istanbul').write_bytes(custom)
            zoneinfo.reset_tzpath([tree])
            try:
                bounds = account_bounds([{'account_id': 'ist', 'timezone': 'Europe/Istanbul'}],
                                        {'period_start_local': '2026-09-01', 'period_end_local_exclusive': '2026-10-01',
                                         'late_cutoff_hours_after_period_end': 48})['ist']
                run = run_fixture()
            finally:
                zoneinfo.reset_tzpath(original)
        self.assertIs(ZoneInfo('Europe/Istanbul'), cached)   # the cache never noticed; a run trusting it would bill on +03:00
        self.assertEqual(bounds.start, datetime(2026, 8, 31, 19, tzinfo=timezone.utc))   # the +05:00 rules of the file
        self.assertEqual(bounds.cutoff, datetime(2026, 10, 2, 19, tzinfo=timezone.utc))
        self.assertEqual(bounds.zone.sha256, hashlib.sha256(custom).hexdigest())
        record = run.manifest['versions']['timezones']['Europe/Istanbul']
        self.assertEqual(record, {'source': 'system', 'version': '(version file absent)', 'sha256': hashlib.sha256(custom).hexdigest()})
        # The run really followed those bytes: at +05:00 the period is [Aug 31 19:00Z, Sep 30 19:00Z), so the
        # Aug 31 20:59:59Z event (a4) is inside it and both Sep 30 20:59:59Z events (a5, a6) are past its end;
        # on the cached +03:00 rules the counts would be 1 out of period and 1 late.
        self.assertEqual(run.manifest['counts']['excluded_out_of_period'], 2)
        self.assertEqual(run.manifest['counts']['excluded_late'], 0)
        self.assertEqual(run.manifest['counts']['accepted'], 8)
        self.assertEqual(run.audit['invoices']['acct_a']['usage_sources']['api_calls'][-1]['event_id'], 'a4')
        for key in ('America/New_York', 'Asia/Tokyo', 'UTC'):
            self.assertEqual(run.manifest['versions']['timezones'][key]['source'], 'tzdata package', key)
        from rvn_ledger.audit import reconcile
        reconcile(run.invoices, run.quarantine, run.audit, run.manifest)

    def test_provenance_is_taken_from_the_bytes_the_rules_were_built_from_not_a_later_read(self):
        import zoneinfo
        from rvn_ledger.timing import resolve_zone, timezone_provenance, zone_provenance
        before, after = self.fixed_offset_tzif(5 * 3600), self.fixed_offset_tzif(-2 * 3600, b'OTH')
        original = zoneinfo.TZPATH
        with tempfile.TemporaryDirectory(dir=SCRATCH) as tmp:
            tree = Path(tmp) / 'tz'
            tree.mkdir()
            (tree / 'UTC').write_bytes(before)
            zoneinfo.reset_tzpath([tree])
            try:
                resolved = resolve_zone('UTC')
                (tree / 'UTC').write_bytes(after)          # the file changes after the rules were resolved
                self.assertEqual(timezone_provenance([resolved]), {'UTC': {'source': 'system', 'version': '(version file absent)', 'sha256': hashlib.sha256(before).hexdigest()}})
                self.assertEqual(datetime(2026, 9, 1, tzinfo=resolved.tz).utcoffset(), timedelta(hours=5))
                self.assertEqual(zone_provenance('UTC')['sha256'], hashlib.sha256(after).hexdigest())   # a fresh read is a different claim
                again = resolve_zone('UTC')
                self.assertEqual(again.sha256, hashlib.sha256(after).hexdigest())
                self.assertEqual(datetime(2026, 9, 1, tzinfo=again.tz).utcoffset(), timedelta(hours=-2))
                with self.assertRaises(RuntimeError):      # one run may not record two different rule sets under one key
                    timezone_provenance([resolved, again])
            finally:
                zoneinfo.reset_tzpath(original)

    @unittest.skipUnless(importlib.util.find_spec('tzdata'), 'optional tzdata package absent; the other fixture zones need it')
    def test_pipeline_records_the_bytes_its_bounds_were_built_from_not_a_later_read(self):
        # End-to-end form of the previous test: the zone file changes right after the context resolved the
        # bounds, and the manifest must still hash the bytes that billed the run. A manifest built by
        # re-reading TZPATH at the end of the run would describe rules nobody used.
        import zoneinfo
        from rvn_ledger import pipeline
        before, after = self.fixed_offset_tzif(5 * 3600), self.fixed_offset_tzif(-2 * 3600, b'OTH')
        original = zoneinfo.TZPATH
        with tempfile.TemporaryDirectory(dir=SCRATCH) as tmp:
            tree = Path(tmp) / 'tz'
            (tree / 'Europe').mkdir(parents=True)
            (tree / 'Europe' / 'Istanbul').write_bytes(before)
            real_prepare = pipeline.prepare_context

            def prepare_then_change_file(*args, **kwargs):
                context = real_prepare(*args, **kwargs)
                (tree / 'Europe' / 'Istanbul').write_bytes(after)
                return context
            zoneinfo.reset_tzpath([tree])
            try:
                with patch('rvn_ledger.pipeline.prepare_context', side_effect=prepare_then_change_file):
                    run = run_fixture()
            finally:
                zoneinfo.reset_tzpath(original)
        self.assertEqual(run.manifest['versions']['timezones']['Europe/Istanbul']['sha256'], hashlib.sha256(before).hexdigest())
        self.assertEqual(run.manifest['counts']['excluded_out_of_period'], 2)   # the +05:00 rules of `before`, as above
        self.assertEqual(run.manifest['counts']['excluded_late'], 0)

    def test_every_account_in_one_zone_shares_one_resolution(self):
        from rvn_ledger.timing import account_bounds
        bounds = account_bounds([{'account_id': 'a', 'timezone': 'UTC'}, {'account_id': 'b', 'timezone': 'UTC'}, {'account_id': 'c', 'timezone': 'Europe/Istanbul'}],
                                {'period_start_local': '2026-09-01', 'period_end_local_exclusive': '2026-10-01', 'late_cutoff_hours_after_period_end': 48})
        self.assertIs(bounds['a'].zone, bounds['b'].zone)
        self.assertEqual((bounds['a'].zone.key, bounds['c'].zone.key), ('UTC', 'Europe/Istanbul'))
        self.assertTrue(len(bounds['c'].zone.sha256) == 64)
        run = run_fixture()
        self.assertEqual(run.manifest['versions']['timezones']['UTC']['sha256'], bounds['a'].zone.sha256)
