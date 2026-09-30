import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from rvn_ledger.cli import main, parse_args

class PackagingTests(unittest.TestCase):
    def test_human_output_on_windows_codepage(self):
        from test_pipeline import write_fixture
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
        with contextlib.redirect_stdout(io.StringIO()) as s, self.assertRaises(SystemExit) as e:
            main(['--version'])
        self.assertEqual(e.exception.code, 0)
        self.assertIn('1.1.0', s.getvalue())

    def test_missing_input(self):
        with tempfile.TemporaryDirectory() as d, contextlib.redirect_stderr(io.StringIO()) as s:
            self.assertEqual(main(['run', '--input-dir', d, '--out', str(Path(d)/'out')]), 2)
            self.assertFalse((Path(d)/'out').exists())
        self.assertIn('cannot read', s.getvalue())
