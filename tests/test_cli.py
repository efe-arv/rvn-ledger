import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from rvn_ledger.cli import main, parse_args

from fixtures import CLI_ARGS, run_cli, SCRATCH, write_fixture


class CommandLineTests(unittest.TestCase):
    def test_human_output_on_windows_codepage(self):
        with tempfile.TemporaryDirectory() as d:
            inputs = write_fixture(Path(d) / 'in')
            raw = io.BytesIO()
            stream = io.TextIOWrapper(raw, encoding='cp1252')
            with contextlib.redirect_stdout(stream):
                self.assertEqual(main(['run', '--input-dir', str(inputs), '--out', str(Path(d)/'out')]), 0)
                self.assertEqual(main(['check', '--out', str(Path(d)/'out')]), 0)
            stream.flush()
            self.assertIn(b'OK:', raw.getvalue())

    def test_defaults(self):
        a = parse_args(['run'])
        self.assertEqual(a.input_dir, Path('data'))
        self.assertEqual(a.out, Path('out'))
        self.assertFalse(a.json)

    def test_alias(self):
        self.assertEqual(parse_args(['-run', '--json']).command, 'run')
        self.assertEqual(parse_args(['-check']).command, 'check')

    def test_help(self):
        with contextlib.redirect_stdout(io.StringIO()) as s, self.assertRaises(SystemExit) as e:
            main(['-help'])
        self.assertEqual(e.exception.code, 0)
        self.assertIn('rvn-ledger', s.getvalue())

    def test_version(self):
        from rvn_ledger import __version__
        with contextlib.redirect_stdout(io.StringIO()) as s, self.assertRaises(SystemExit) as e:
            main(['--version'])
        self.assertEqual(e.exception.code, 0)
        self.assertIn(__version__, s.getvalue())

    def test_missing_input(self):
        with tempfile.TemporaryDirectory() as d, contextlib.redirect_stderr(io.StringIO()) as s:
            self.assertEqual(main(['run', '--input-dir', d, '--out', str(Path(d)/'out')]), 2)
            self.assertFalse((Path(d)/'out').exists())
        self.assertIn('cannot read', s.getvalue())

    def test_cp1252_human_and_json_all_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); inputs = write_fixture(root / 'in'); out = root / 'fatura-東京'
            env = {**os.environ, 'PYTHONIOENCODING': 'cp1252:strict'}
            def invoke(*args):
                return subprocess.run([*CLI_ARGS, *map(str, args)], env=env, capture_output=True)
            for json_mode in (False, True):
                extra = ('--json',) if json_mode else ()
                commands = [('run', '--input-dir', inputs, '--out', out), ('check', '--out', out),
                            ('explain', '--out', out, '--events', inputs / 'events.jsonl')]
                for command in commands:
                    with self.subTest(command=command, json=json_mode):
                        result = invoke(*command, *extra)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertNotIn(b'Traceback', result.stderr)
                        if json_mode:
                            value = json.loads(result.stdout)
                            if command[0] in ('run', 'check'):
                                self.assertEqual(value['out'], str(out))
                error = invoke('run', '--input-dir', root / 'missing-東京', '--out', out)
                self.assertEqual(error.returncode, 2)
                self.assertNotIn(b'Traceback', error.stderr)


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
            stream.flush()
            text = raw.getvalue().decode('cp1252')
            self.assertEqual(text.count('OK:'), 2, text)   # run and check; explain prints records
            self.assertIn('events.jsonl:8 event="a7" status=quarantined', text)
            self.assertIn(self.ESCAPED, text)
            self.assertNotIn(self.NAME, text)
            self.assertTrue((out / 'manifest.json').is_file())

    def test_real_cp1252_stdout_in_a_subprocess(self):
        env = {**os.environ, 'PYTHONIOENCODING': 'cp1252'}
        with tempfile.TemporaryDirectory(dir=SCRATCH) as tmp:
            inputs = write_fixture(Path(tmp) / 'in')
            out = Path(tmp) / self.NAME
            for args in (('run', '--input-dir', inputs, '--out', out), ('check', '--out', out), ('explain', '--out', out)):
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


class ExplainFailureLabelTests(unittest.TestCase):
    """`explain` never publishes: its failures must not be reported as a failed publication."""

    def published(self, tmp):
        inputs = write_fixture(Path(tmp) / 'in')
        out = Path(tmp) / 'out'
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['run', '--input-dir', str(inputs), '--out', str(out)]), 0)
        return inputs, out

    def explain(self, *argv):
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            code = main(['explain', *argv])
        return code, stderr.getvalue()

    def test_unreadable_events_file_is_an_input_error(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as tmp:
            inputs, out = self.published(tmp)
            code, text = self.explain('--out', str(out), '--events', str(Path(tmp) / 'missing.jsonl'))
            self.assertEqual(code, 2)
            self.assertTrue(text.startswith('error: --events: cannot read '), text)
            self.assertNotIn('publication failed', text)
            code, text = self.explain('--out', str(out), '--events', str(inputs))   # a directory, not a file
            self.assertEqual(code, 2)
            self.assertTrue(text.startswith('error: --events: cannot read '), text)

    def test_output_set_problems_are_check_failures(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as tmp:
            inputs, out = self.published(tmp)
            (out / 'invoices.json').write_bytes(b'[]\n')
            code, text = self.explain('--out', str(out))
            self.assertEqual(code, 2)
            self.assertTrue(text.startswith('check failed: '), text)
            code, text = self.explain('--out', str(Path(tmp) / 'never-published'))
            self.assertEqual(code, 2)
            self.assertTrue(text.startswith('check failed: '), text)
            self.assertNotIn('publication failed', text)
            # `run` keeps its own label: a publication that cannot proceed is still a publication failure.
            blocked = Path(tmp) / 'blocked'
            blocked.write_bytes(b'keep')
            with contextlib.redirect_stderr(io.StringIO()) as stderr:
                self.assertEqual(main(['run', '--input-dir', str(inputs), '--out', str(blocked)]), 2)
            self.assertTrue(stderr.getvalue().startswith('publication failed: '), stderr.getvalue())


class TimezoneConfigurationTests(unittest.TestCase):
    def test_non_string_timezones_fail_cleanly_before_publication(self):
        from fixtures import ACCOUNTS
        for zone in ([], {}, ['UTC'], {'key': 'UTC'}, None, 1, True):
            with self.subTest(zone=zone), tempfile.TemporaryDirectory(dir=SCRATCH) as tmp:
                accounts = [{**ACCOUNTS[0], 'timezone': zone}]
                inputs = write_fixture(Path(tmp) / 'in', accounts=accounts, raw=b'')
                out = Path(tmp) / 'out'
                result = run_cli('run', '--input-dir', inputs, '--out', out)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertEqual(result.stdout, '')
                self.assertTrue(result.stderr.startswith('error:'), result.stderr)
                self.assertNotIn('Traceback', result.stderr)
                self.assertFalse(out.exists())
