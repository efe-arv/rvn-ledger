import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from verify_support import artifact_hashes

from rvn_ledger.inputs import read_events

import test_selection
from fixtures import ROOT, SCRATCH


class VerifySupportTests(unittest.TestCase):
    def test_artifact_hashes_cover_sources_and_own_log_but_not_other_gates_rewritten_logs(self):
        # Review N5: every verify script rewrites its own suite log, so a receipt that hashes
        # another gate's suite log goes stale as soon as that gate runs again. Sources, tests,
        # documents, preserved red/green logs and the gate's own log are hashed; other gates'
        # regenerated outputs are excluded by name.
        from verify_support import artifact_hashes
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'tests').mkdir()
            files = {'money.py': b'x = 1\n', 'tests/test_money.py': b'import unittest\n', 'notes.md': b'# doc\n',
                     'money-red.log': b'FAILED\n', 'money-green.log': b'OK\n',
                     'timing-verify-suite.log': b'Ran 1 test in 0.001s\n', 'aggregation-verify-suite.log': b'Ran 1 test in 0.002s\n',
                     'subscription-verify-suite.log': b'Ran 1 test in 0.003s\n', 'timing.receipt.json': b'{}\n'}
            for name, data in files.items():
                (root / name).write_bytes(data)
            hashes = artifact_hashes(root, own_outputs=('timing-verify-suite.log',))
            self.assertEqual(sorted(hashes), ['money-green.log', 'money-red.log', 'money.py', 'notes.md',
                                              'tests/test_money.py', 'timing-verify-suite.log'])
            for name, digest in hashes.items():
                self.assertEqual(digest, hashlib.sha256(files[name]).hexdigest())
            self.assertNotIn('aggregation-verify-suite.log', artifact_hashes(root))
            self.assertNotIn('timing-verify-suite.log', artifact_hashes(root))


class TimingOracleTests(unittest.TestCase):
    # Review N4: the timing verifier's independent check must derive every line's status
    # on its own (parse, identity, first copy, field validity, local period, late cutoff)
    # so a valid winner given the wrong status by the pipeline is noticed.
    fixture = test_selection.ClassificationTests()

    def test_oracle_reproduces_hand_derived_statuses_for_every_line(self):
        from verify_timing import independent_statuses
        f = self.fixture
        lines = [
            f.event(event_id='ok', ingest_seq=1),                                        # 1 accepted
            f.event(event_id='ok', ingest_seq=2, units=999),                             # 2 duplicate_ignored
            f.event(event_id='early', ingest_seq=3, ts='2026-08-31T20:59:59Z'),          # 3 Istanbul start is 08-31T21:00Z
            f.event(event_id='late', ingest_seq=4, ingested_at='2026-10-02T21:00:01Z'),  # 4 Istanbul end 09-30T21:00Z + 48h
            f.event(event_id='ghost', ingest_seq=5, account_id='nobody'),                # 5 quarantined
            f.event(event_id='zero', ingest_seq=6, units=0, metric='bad'),               # 6 quarantined
            f.event(event_id='bool', ingest_seq=7, units=True),                          # 7 quarantined (bool units)
            f.event(event_id='naive', ingest_seq=8, ts='2026-09-10T00:00:00'),           # 8 quarantined (naive ts)
            f.event(event_id='ok', ingest_seq=0, units=7),                               # 9 wins over line 1: smaller ingest_seq
        ]
        raw = ('\n'.join(json.dumps(v) for v in lines) + '\n').encode()
        raw += b'{broken\n'                                                                # 10 quarantined, no identity
        raw += b'{"event_id":"ok","ingest_seq":-1,"units":NaN,"meta":{"event_id":"x","event_id":"y"}}\n'  # 11 strict-rejected first copy of ok
        raw += b'{"event_id":"ok","ingest_seq":-2,"event_id":"ok"}\n'                     # 12 ambiguous identity: cannot claim ok
        raw += b'[]\n'                                                                    # 13 not an object
        raw += b'{"event_id":"solo","ingest_seq":' + b'9' * 5000 + b'}\n'                 # 14 over-long ingest_seq: no identity
        expected = {1: 'duplicate_ignored', 2: 'duplicate_ignored', 3: 'excluded_out_of_period', 4: 'excluded_late',
                    5: 'quarantined', 6: 'quarantined', 7: 'quarantined', 8: 'quarantined', 9: 'duplicate_ignored',
                    10: 'quarantined', 11: 'quarantined', 12: 'quarantined', 13: 'quarantined', 14: 'quarantined'}
        self.assertEqual(independent_statuses(raw, f.accounts, f.period), expected)

    def test_oracle_agrees_with_pipeline_and_notices_a_misclassified_winner(self):
        from rvn_ledger.selection import classify_events
        from verify_timing import independent_statuses
        f = self.fixture
        raw = ('\n'.join(json.dumps(v) for v in [f.event(event_id='a', ingest_seq=1), f.event(event_id='b', ingest_seq=2, units=3)]) + '\n').encode()
        pipeline = classify_events(read_events(raw), f.accounts, f.period).statuses
        oracle = independent_statuses(raw, f.accounts, f.period)
        self.assertEqual(oracle, pipeline)
        self.assertEqual(oracle, {1: 'accepted', 2: 'accepted'})
        self.assertEqual(independent_statuses(b'', f.accounts, f.period), {})

    def test_compare_statuses_refuses_a_misclassified_winner(self):
        # Review F2: the old "notices a misclassified winner" assertion was a tautology and the
        # mismatch path in main() had no unit test. The comparison is now a function that main()
        # calls; it must raise VerificationError naming the line and both statuses.
        from verify_support import VerificationError
        from verify_timing import compare_statuses
        pipeline = {1: 'accepted', 2: 'accepted', 3: 'quarantined'}
        self.assertEqual(compare_statuses(pipeline, dict(pipeline)), [])
        with self.assertRaisesRegex(VerificationError, r"\(2, 'quarantined', 'accepted'\)"):
            compare_statuses({**pipeline, 2: 'quarantined'}, pipeline)
        with self.assertRaisesRegex(VerificationError, r"\(3, 'quarantined', 'accepted'\)"):
            compare_statuses(pipeline, {**pipeline, 3: 'accepted'})
        for other in ({1: 'accepted', 2: 'accepted'}, {**pipeline, 4: 'accepted'}, {}):
            with self.subTest(other=other), self.assertRaisesRegex(VerificationError, 'set of raw lines'):
                compare_statuses(pipeline, other)

    def test_oracle_handles_extreme_timestamps_like_the_product(self):
        # Review F1: year-0001 / year-9999 instants overflow when converted to another zone.
        # The product quarantines an unconvertible timestamp (invalid_ts / invalid_ingested_at)
        # and compares UTC instants; the oracle must agree instead of dying with OverflowError.
        from rvn_ledger.selection import classify_events
        from verify_timing import independent_statuses, instant
        accounts = [{'account_id': 'ny', 'timezone': 'America/New_York'}, {'account_id': 'tokyo', 'timezone': 'Asia/Tokyo'},
                    {'account_id': 'utc', 'timezone': 'UTC'}]
        f = self.fixture
        lines = [
            f.event(event_id='a', ingest_seq=1, account_id='utc', ts='0001-01-01T00:30:00+01:00'),          # invalid_ts
            f.event(event_id='b', ingest_seq=2, account_id='ny', ts='0001-01-01T00:00:00Z'),                # out of period
            f.event(event_id='c', ingest_seq=3, account_id='tokyo', ts='9999-12-31T23:00:00Z'),             # out of period
            f.event(event_id='d', ingest_seq=4, account_id='utc', ingested_at='9999-12-31T23:30:00-01:00'),  # invalid_ingested_at
            f.event(event_id='e', ingest_seq=5, account_id='ny', ts='9999-12-31T23:59:59Z', ingested_at='0001-01-01T00:00:00Z'),
            f.event(event_id='f', ingest_seq=6, account_id='utc'),                                          # control: accepted
        ]
        raw = ('\n'.join(json.dumps(v) for v in lines) + '\n').encode()
        pipeline = classify_events(read_events(raw), accounts, f.period)
        self.assertEqual(pipeline.statuses, {1: 'quarantined', 2: 'excluded_out_of_period', 3: 'excluded_out_of_period',
                                             4: 'quarantined', 5: 'excluded_out_of_period', 6: 'accepted'})
        self.assertEqual(pipeline.reasons, {1: ('invalid_ts',), 4: ('invalid_ingested_at',)})
        self.assertEqual(independent_statuses(raw, accounts, f.period), pipeline.statuses)
        self.assertIsNone(instant('0001-01-01T00:30:00+01:00'))
        self.assertIsNone(instant('9999-12-31T23:30:00-01:00'))


class ReceiptHashScopeTests(unittest.TestCase):
    def test_patterns_bind_the_installed_package_sources_and_lockfiles(self):
        from verify_support import HASHED_PATTERNS, artifact_hashes, hash_scope
        for pattern in ('src/**/*.py', 'pyproject.toml', 'uv.lock', '*.py', 'tests/*.py', '*.log', '*.md'):
            self.assertIn(pattern, HASHED_PATTERNS)
        hashes = artifact_hashes(ROOT)
        for name in ('src/rvn_ledger/money.py', 'src/rvn_ledger/__init__.py', 'pyproject.toml', 'uv.lock',
                     'verify_support.py', 'tests/test_money.py', 'README.md'):
            self.assertIn(name, hashes)
            self.assertEqual(hashes[name], hashlib.sha256((ROOT / name).read_bytes()).hexdigest(), name)
        for module in sorted((ROOT / 'src' / 'rvn_ledger').glob('*.py')):
            self.assertIn(f'src/rvn_ledger/{module.name}', hashes)
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


class ReceiptBindingTests(unittest.TestCase):
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

    def test_oracle_keeps_submicrosecond_precision_and_unicode_policy(self):
        import test_selection
        from verify_timing import independent_statuses
        fixture = test_selection.ClassificationTests()
        events = [fixture.event(event_id='late', ingested_at='2026-10-02T21:00:00.0000001Z'),
                  fixture.event(event_id='invalid', extra='\ud800')]
        raw = ('\n'.join(json.dumps(event) for event in events) + '\n').encode()
        self.assertEqual(independent_statuses(raw, fixture.accounts, fixture.period),
                         {1: 'excluded_late', 2: 'quarantined'})
