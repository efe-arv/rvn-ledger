"""Review regressions: actual manifest, package tree, and encoded subprocess output."""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from test_pipeline import ROOT, CLI, write_fixture, run_cli
from verify_support import artifact_hashes


class ReviewFixTests(unittest.TestCase):
    def test_manifest_inputs_reject_missing_and_malformed_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            inputs = write_fixture(Path(tmp) / 'in')
            out = Path(tmp) / 'out'
            self.assertEqual(run_cli('run', '--input-dir', inputs, '--out', out).returncode, 0)
            original = json.loads((out / 'manifest.json').read_bytes())
            mutations = []
            for name in original['inputs']:
                missing = copy.deepcopy(original['inputs']); del missing[name]
                mutations.append(missing)
                for bad in (None, [], '', 1):
                    changed = copy.deepcopy(original['inputs']); changed[name] = bad
                    mutations.append(changed)
                for field, values in {'sha256': (None, 1, '', 'g'*64, 'a'*63, 'A'*64),
                                      'bytes': (None, True, -1, 1.5, '1'),
                                      'records': (None, False, -1, 1.5, '1')}.items():
                    changed = copy.deepcopy(original['inputs']); del changed[name][field]
                    mutations.append(changed)
                    for bad in values:
                        changed = copy.deepcopy(original['inputs']); changed[name][field] = bad
                        mutations.append(changed)
            mutations.extend((None, [], {}, {**original['inputs'], 'extra.json': original['inputs']['period.json']}))
            for metadata in mutations:
                with self.subTest(metadata=metadata):
                    (out / 'manifest.json').write_text(json.dumps({**original, 'inputs': metadata}))
                    for command in (('check', '--out', out), ('explain', '--out', out, '--events', inputs / 'events.jsonl')):
                        result = run_cli(*command)
                        self.assertNotEqual(result.returncode, 0)
                        self.assertNotIn('Traceback', result.stderr)

    def test_hashes_bind_actual_package_and_track_mutations(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shutil.copytree(ROOT / 'src', root / 'src')
            for name in ('pyproject.toml', 'uv.lock'):
                shutil.copy2(ROOT / name, root / name)
            before = artifact_hashes(root)
            expected = {p.relative_to(root).as_posix() for p in (root / 'src').rglob('*.py')}
            self.assertTrue(expected <= set(before))
            self.assertTrue({'pyproject.toml', 'uv.lock'} <= set(before))
            money = root / 'src/rvn_ledger/money.py'
            money.write_bytes(money.read_bytes() + b'\n# receipt mutation\n')
            self.assertNotEqual(before['src/rvn_ledger/money.py'], artifact_hashes(root)['src/rvn_ledger/money.py'])
            added = root / 'src/rvn_ledger/added.py'; added.write_text('# added\n')
            self.assertIn('src/rvn_ledger/added.py', artifact_hashes(root))
            money.unlink()
            self.assertNotIn('src/rvn_ledger/money.py', artifact_hashes(root))

    def test_cp1252_human_and_json_all_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); inputs = write_fixture(root / 'in'); out = root / 'fatura-東京'
            env = {**os.environ, 'PYTHONIOENCODING': 'cp1252:strict'}
            def invoke(*args):
                return subprocess.run([sys.executable, str(CLI), *map(str, args)], env=env, capture_output=True)
            for json_mode in (False, True):
                extra = ('--json',) if json_mode else ()
                workbook = root / f'inputs-東京-{json_mode}.xlsx'
                imported = root / f'import-東京-{json_mode}'
                commands = [('run', '--input-dir', inputs, '--out', out), ('check', '--out', out),
                            ('explain', '--out', out, '--events', inputs / 'events.jsonl'),
                            ('export', '--kind', 'report', '--out', out, '--file', root / f'report-東京-{json_mode}.xlsx'),
                            ('export', '--kind', 'inputs', '--input-dir', inputs, '--file', workbook),
                            ('import', '--file', workbook, '--input-dir', imported)]
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
