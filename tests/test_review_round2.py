"""Regressions for the second review round.

P2 #1  manifest['inputs'] must name exactly the four inputs, each with sha256/bytes/records of the
       right type and value; `check` and `explain` fail closed (exit 2, no traceback) on any violation.
P2 #2  verify receipts must hash the installed package sources (src/**/*.py), pyproject.toml and uv.lock.
P3 #3  human CLI output never raises UnicodeEncodeError on a legacy stdout encoding; JSON output is unchanged.
"""
import contextlib
import hashlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from test_audit import run_fixture
from test_pipeline import ROOT, write_fixture, run_cli

INPUT_NAMES = ('events.jsonl', 'accounts.json', 'plans.json', 'period.json')
SCRATCH = os.environ.get('TMPDIR')


def publish_run(run, directory):
    from rvn_ledger.outputs import publish, serialize
    publish(directory, {'invoices.json': serialize(run.invoices), 'quarantine.json': serialize(run.quarantine),
                        'audit.json': serialize(run.audit)}, run.manifest)


def rewrite_manifest(out, mutate):
    """Edit a published manifest in place; manifest.json carries no self-hash, so only reconciliation can notice."""
    path = Path(out) / 'manifest.json'
    manifest = json.loads(path.read_bytes())
    mutate(manifest)
    path.write_bytes(json.dumps(manifest, indent=2).encode() + b'\n')


class ManifestInputMetadataTests(unittest.TestCase):
    def rejected(self, mutate, case):
        from rvn_ledger.audit import AuditError, reconcile
        run = run_fixture()
        mutate(run.manifest)
        with self.subTest(case=case), self.assertRaises(AuditError):
            reconcile(run.invoices, run.quarantine, run.audit, run.manifest)

    def test_actual_manifest_reconciles_and_has_the_expected_schema(self):
        from rvn_ledger.audit import reconcile
        run = run_fixture()
        self.assertEqual(tuple(run.manifest['inputs']), INPUT_NAMES)
        for name, meta in run.manifest['inputs'].items():
            self.assertEqual(set(meta), {'sha256', 'bytes', 'records'}, name)
        reconcile(run.invoices, run.quarantine, run.audit, run.manifest)

    def test_every_required_input_entry_is_required_and_nothing_else_is_allowed(self):
        for name in INPUT_NAMES:
            self.rejected(lambda m, name=name: m['inputs'].pop(name), f'missing {name}')
            self.rejected(lambda m, name=name: m['inputs'].__setitem__(name, None), f'null {name}')
        self.rejected(lambda m: m['inputs'].__setitem__('extra.json', dict(m['inputs']['period.json'])), 'extra input name')
        self.rejected(lambda m: m['inputs'].__setitem__('Events.jsonl', m['inputs'].pop('events.jsonl')), 'renamed input')
        self.rejected(lambda m: m.__setitem__('inputs', {}), 'empty inputs')
        self.rejected(lambda m: m.__setitem__('inputs', []), 'inputs is a list')
        self.rejected(lambda m: m.__setitem__('inputs', None), 'inputs is null')

    def test_entry_shapes_and_field_types_are_validated(self):
        run = run_fixture()
        good = run.manifest['inputs']['events.jsonl']
        digest = good['sha256']
        bad_entries = (None, [], 'text', 7, True, {}, {'sha256': digest}, {'sha256': digest, 'bytes': good['bytes']},
                       {**good, 'extra': 1}, {'sha256': digest, 'bytes': good['bytes'], 'record': good['records']})
        for name in INPUT_NAMES:
            for bad in bad_entries:
                self.rejected(lambda m, name=name, bad=bad: m['inputs'].__setitem__(name, bad), f'{name} entry {bad!r}')
        bad_values = {
            'sha256': (None, 1, True, [], 'abc', digest.upper(), digest[:-1], digest + '0', 'g' * 64, digest[:-1] + 'G'),
            'bytes': (None, True, False, -1, '12', 1.0, 12.0, [], {}),
            'records': (None, True, False, -1, '1', 1.5, 0.0, [], {}),
        }
        for field, values in bad_values.items():
            for bad in values:
                for name in INPUT_NAMES:
                    self.rejected(lambda m, name=name, field=field, bad=bad: m['inputs'][name].__setitem__(field, bad),
                                  f'{name}.{field} = {bad!r}')

    def test_record_counts_reconcile_with_the_run(self):
        # events.jsonl records are the raw lines; period.json is one object; accounts.json yields one invoice each;
        # plans.json must hold at least one plan. Each is a fact of the run, so the manifest cannot contradict it.
        self.rejected(lambda m: m['inputs']['events.jsonl'].__setitem__('records', m['counts']['raw'] + 1), 'events records != raw')
        self.rejected(lambda m: m['inputs']['events.jsonl'].__setitem__('records', 0), 'events records zero')
        self.rejected(lambda m: m['inputs']['period.json'].__setitem__('records', 2), 'period records')
        self.rejected(lambda m: m['inputs']['accounts.json'].__setitem__('records', m['counts']['invoices'] + 1), 'accounts records != invoices')
        self.rejected(lambda m: m['inputs']['plans.json'].__setitem__('records', 0), 'plans records zero')

    def test_cli_check_and_explain_fail_closed_without_an_input_entry(self):
        for name in INPUT_NAMES:
            with self.subTest(missing=name), tempfile.TemporaryDirectory(dir=SCRATCH) as tmp:
                inputs = write_fixture(Path(tmp) / 'in')
                out = Path(tmp) / 'out'
                self.assertEqual(run_cli('run', '--input-dir', inputs, '--out', out).returncode, 0)
                rewrite_manifest(out, lambda m, name=name: m['inputs'].pop(name))
                for args in (('check', '--out', out), ('check', '--out', out, '--json'),
                             ('explain', '--out', out), ('explain', '--out', out, '--json'),
                             ('explain', '--out', out, '--events', inputs / 'events.jsonl'),
                             ('explain', '--out', out, '--line', '1', '--events', inputs / 'events.jsonl'),
                             ('export', '--out', out, '--file', Path(tmp) / f'{name}.xlsx')):
                    result = run_cli(*args)
                    self.assertEqual(result.returncode, 2, (args, result.stdout, result.stderr))
                    self.assertNotIn('Traceback', result.stderr, args)
                    self.assertNotIn('KeyError', result.stderr, args)
                    self.assertIn('check failed', result.stderr, args)
                    self.assertEqual(result.stdout, '', args)
                self.assertFalse((Path(tmp) / f'{name}.xlsx').exists())

    def test_cli_check_and_explain_fail_closed_on_bad_input_field_types(self):
        mutations = [
            ('null entry', lambda m: m['inputs'].__setitem__('accounts.json', None)),
            ('list entry', lambda m: m['inputs'].__setitem__('plans.json', [])),
            ('null sha256', lambda m: m['inputs']['events.jsonl'].__setitem__('sha256', None)),
            ('short sha256', lambda m: m['inputs']['events.jsonl'].__setitem__('sha256', 'abc')),
            ('bool bytes', lambda m: m['inputs']['period.json'].__setitem__('bytes', True)),
            ('negative bytes', lambda m: m['inputs']['period.json'].__setitem__('bytes', -1)),
            ('text records', lambda m: m['inputs']['accounts.json'].__setitem__('records', '4')),
            ('float records', lambda m: m['inputs']['plans.json'].__setitem__('records', 2.0)),
            ('missing records', lambda m: m['inputs']['events.jsonl'].pop('records')),
            ('events records != raw', lambda m: m['inputs']['events.jsonl'].__setitem__('records', 1)),
        ]
        with tempfile.TemporaryDirectory(dir=SCRATCH) as tmp:
            inputs = write_fixture(Path(tmp) / 'in')
            good = Path(tmp) / 'good'
            self.assertEqual(run_cli('run', '--input-dir', inputs, '--out', good).returncode, 0)
            for case, mutate in mutations:
                with self.subTest(case=case):
                    out = Path(tmp) / case.replace(' ', '-')
                    out.mkdir()
                    for name in ('invoices.json', 'quarantine.json', 'audit.json', 'manifest.json'):
                        (out / name).write_bytes((good / name).read_bytes())
                    rewrite_manifest(out, mutate)
                    for args in (('check', '--out', out), ('explain', '--out', out, '--events', inputs / 'events.jsonl')):
                        result = run_cli(*args)
                        self.assertEqual(result.returncode, 2, (args, result.stderr))
                        self.assertNotIn('Traceback', result.stderr, args)
                        self.assertIn('check failed', result.stderr, args)
            self.assertEqual(run_cli('check', '--out', good).returncode, 0)   # the untouched copy still passes


class ReceiptHashScopeTests(unittest.TestCase):
    def test_patterns_bind_the_installed_package_sources_and_lockfiles(self):
        from rvn_ledger.pipeline import CODE_MODULES
        from verify_support import HASHED_PATTERNS, artifact_hashes, hash_scope
        for pattern in ('src/**/*.py', 'pyproject.toml', 'uv.lock', '*.py', 'tests/*.py', '*.log', '*.md'):
            self.assertIn(pattern, HASHED_PATTERNS)
        hashes = artifact_hashes(ROOT)
        for name in ('src/rvn_ledger/money.py', 'src/rvn_ledger/__init__.py', 'pyproject.toml', 'uv.lock',
                     'verify_support.py', 'cli.py', 'tests/test_money.py', 'README.md'):
            self.assertIn(name, hashes)
            self.assertEqual(hashes[name], hashlib.sha256((ROOT / name).read_bytes()).hexdigest(), name)
        for module in CODE_MODULES:
            self.assertIn(f'src/rvn_ledger/{module}', hashes)
        self.assertFalse([n for n in hashes if '__pycache__' in n or n.endswith('.pyc') or n.startswith('.venv')], hashes)
        self.assertEqual(hash_scope(ROOT)['patterns'], list(HASHED_PATTERNS))

    def test_mutation_addition_and_removal_of_package_sources_change_the_hashes(self):
        from verify_support import artifact_hashes
        with tempfile.TemporaryDirectory(dir=SCRATCH) as tmp:
            root = Path(tmp)
            (root / 'src' / 'rvn_ledger' / '__pycache__').mkdir(parents=True)
            (root / 'tests').mkdir()
            files = {'src/rvn_ledger/__init__.py': b'__version__ = "1"\n', 'src/rvn_ledger/money.py': b'def half_up(n, d): return n // d\n',
                     'src/rvn_ledger/__pycache__/money.cpython-311.pyc': b'\x00bytecode', 'pyproject.toml': b'[project]\nname = "x"\n',
                     'uv.lock': b'version = 1\n', 'cli.py': b'print(1)\n', 'tests/test_money.py': b'import unittest\n', 'README.md': b'# r\n',
                     'timing-verify-suite.log': b'Ran 1 test\n'}
            for name, data in files.items():
                (root / name).write_bytes(data)
            before = artifact_hashes(root, own_outputs=('timing-verify-suite.log',))
            self.assertEqual(sorted(before), ['README.md', 'cli.py', 'pyproject.toml', 'src/rvn_ledger/__init__.py', 'src/rvn_ledger/money.py',
                                              'tests/test_money.py', 'timing-verify-suite.log', 'uv.lock'])
            for name, digest in before.items():
                self.assertEqual(digest, hashlib.sha256(files[name]).hexdigest())
            (root / 'src/rvn_ledger/money.py').write_bytes(b'def half_up(n, d): return (2 * n + d) // (2 * d)\n')
            after = artifact_hashes(root, own_outputs=('timing-verify-suite.log',))
            self.assertNotEqual(after['src/rvn_ledger/money.py'], before['src/rvn_ledger/money.py'])
            self.assertEqual({k: v for k, v in after.items() if k != 'src/rvn_ledger/money.py'},
                             {k: v for k, v in before.items() if k != 'src/rvn_ledger/money.py'})
            (root / 'uv.lock').write_bytes(b'version = 2\n')
            self.assertNotEqual(artifact_hashes(root)['uv.lock'], before['uv.lock'])
            (root / 'src/rvn_ledger/new_module.py').write_bytes(b'x = 1\n')
            (root / 'tests/test_money.py').unlink()
            final = artifact_hashes(root)
            self.assertIn('src/rvn_ledger/new_module.py', final)
            self.assertNotIn('tests/test_money.py', final)


class SafeHumanDisplayTests(unittest.TestCase):
    NAME = 'fatura-東京'
    ESCAPED = 'fatura-\\u6771\\u4eac'

    def cp1252_stdout(self):
        raw = io.BytesIO()
        return raw, io.TextIOWrapper(raw, encoding='cp1252', errors='strict')

    def test_run_check_explain_on_cp1252_stdout_with_a_non_latin_output_path(self):
        from rvn_ledger.cli import main
        with tempfile.TemporaryDirectory(dir=SCRATCH) as tmp:
            inputs = write_fixture(Path(tmp) / 'in')
            out = Path(tmp) / self.NAME
            raw, stream = self.cp1252_stdout()
            with contextlib.redirect_stdout(stream):
                self.assertEqual(main(['run', '--input-dir', str(inputs), '--out', str(out)]), 0)
                self.assertEqual(main(['check', '--out', str(out)]), 0)
                self.assertEqual(main(['explain', '--out', str(out)]), 0)
                self.assertEqual(main(['export', '--out', str(out), '--file', str(Path(tmp) / f'{self.NAME}.xlsx')]), 0)
                self.assertEqual(main(['export', '--kind', 'inputs', '--input-dir', str(inputs), '--file', str(Path(tmp) / f'girdi-{self.NAME}.xlsx')]), 0)
                self.assertEqual(main(['import', '--file', str(Path(tmp) / f'girdi-{self.NAME}.xlsx'), '--input-dir', str(Path(tmp) / f'ice-{self.NAME}')]), 0)
            stream.flush()
            text = raw.getvalue().decode('cp1252')
            self.assertEqual(text.count('OK:'), 5, text)
            self.assertIn('events.jsonl:8 event="a7" status=quarantined', text)
            self.assertIn(self.ESCAPED, text)
            self.assertNotIn(self.NAME, text)
            self.assertTrue((out / 'manifest.json').is_file())

    def test_real_cp1252_stdout_in_a_subprocess(self):
        env = {**os.environ, 'PYTHONIOENCODING': 'cp1252'}
        with tempfile.TemporaryDirectory(dir=SCRATCH) as tmp:
            inputs = write_fixture(Path(tmp) / 'in')
            out = Path(tmp) / self.NAME
            for args in (('run', '--input-dir', inputs, '--out', out), ('check', '--out', out), ('explain', '--out', out),
                         ('export', '--out', out, '--file', Path(tmp) / f'{self.NAME}.xlsx')):
                result = run_cli(*args, env=env)
                self.assertEqual(result.returncode, 0, (args, result.stderr))
                self.assertNotIn('Traceback', result.stderr, args)
                if args[0] == 'explain':
                    self.assertTrue(result.stdout.startswith('events.jsonl:8 event="a7" status=quarantined\n'), result.stdout)
                    self.assertNotIn('OK:', result.stdout)
                else:
                    self.assertIn('OK:', result.stdout, args)
            run = run_cli('run', '--input-dir', inputs, '--out', out, env=env)
            self.assertIn(self.ESCAPED, run.stdout)

    def test_json_summaries_keep_the_exact_path_on_cp1252_stdout(self):
        from rvn_ledger.cli import main
        with tempfile.TemporaryDirectory(dir=SCRATCH) as tmp:
            inputs = write_fixture(Path(tmp) / 'in')
            out = Path(tmp) / self.NAME
            for argv in (['run', '--input-dir', str(inputs), '--out', str(out), '--json'], ['check', '--out', str(out), '--json']):
                raw, stream = self.cp1252_stdout()
                with contextlib.redirect_stdout(stream):
                    self.assertEqual(main(argv), 0)
                stream.flush()
                payload = json.loads(raw.getvalue().decode('cp1252'))
                self.assertEqual(payload['out'], str(out))
            result = run_cli('explain', '--out', out, '--json', env={**os.environ, 'PYTHONIOENCODING': 'cp1252'})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)['count'], 7)

    def test_error_diagnostics_on_a_strict_legacy_stderr_are_safe(self):
        from rvn_ledger.cli import main
        with tempfile.TemporaryDirectory(dir=SCRATCH) as tmp:
            raw, stream = self.cp1252_stdout()
            with contextlib.redirect_stderr(stream):
                code = main(['run', '--input-dir', str(Path(tmp) / self.NAME), '--out', str(Path(tmp) / 'out')])
            stream.flush()
            text = raw.getvalue().decode('cp1252')
            self.assertEqual(code, 2)
            self.assertIn('cannot read', text)
            self.assertIn(self.ESCAPED, text)
            self.assertFalse((Path(tmp) / 'out').exists())

    def test_display_is_lossless_where_the_stream_can_encode(self):
        from rvn_ledger.cli import display
        self.assertEqual(display('fatura-東京 ş', io.TextIOWrapper(io.BytesIO(), encoding='utf-8')), 'fatura-東京 ş')
        self.assertEqual(display('fatura-東京 ş', io.TextIOWrapper(io.BytesIO(), encoding='cp1252')), 'fatura-\\u6771\\u4eac \\u015f')
        self.assertEqual(display('plain', io.TextIOWrapper(io.BytesIO(), encoding='ascii')), 'plain')
        self.assertEqual(display('fatura-東京', io.StringIO()), 'fatura-東京')          # no byte encoding: nothing to escape
        self.assertEqual(display('bad\udcff', io.TextIOWrapper(io.BytesIO(), encoding='utf-8')), 'bad\\udcff')  # undecodable file name byte
