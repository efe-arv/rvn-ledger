"""Shared receipt mechanics for the verify scripts (not a billing module).

Nothing here is a skippable `assert`: every check raises VerificationError, so
`python -O` cannot produce a receipt without running the checks. Every number
written to a receipt is measured by the current run. History this run did not
observe (the original red logs) is recorded as observed-from-log with an
`unknown` exit code, never as a literal exit code.
"""
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import unittest
from pathlib import Path

DATA = Path(__file__).resolve().parent / 'data'
# Historical provenance: the archive hash below was recorded by an earlier run.
# The original attachment.zip is absent; its hash and the reason for its loss cannot
# be independently re-established in this run. The four data/ files are available
# and are checked now against their previously recorded per-file hashes.
ARCHIVE_SHA256 = '785735327e9e1afb179f1a4580d1978f07442ab279cfef27ff09f006c89cbc9d'
INPUT_SHA256 = {
    'events.jsonl': 'f42a4f0712cbf5c096849a9790b233609a1775aaec1810c952dfa095ed637d82',
    'accounts.json': '81997273a9014e5e1a9dd238b178996cb61740d828c8cb370e50f6f289298e4d',
    'period.json': '24c3a0638052b7d296e68625e8674d4d2a12b65a57094bbd1dfb8f7738bc4522',
    'plans.json': '9922b3739ba8104931b22b3da242ba55540b1bdee7ad7f4cb3e06fe51cd4e50f',
}
INPUT_NAMES = tuple(INPUT_SHA256)
SOURCE_PROVENANCE = {
    'archive_sha256': ARCHIVE_SHA256,
    'archive_status': 'original attachment.zip unavailable; archive hash is historical, not reverified; '
                      'data/ files are checked against their previously recorded per-file hashes',
    'input_sha256': dict(INPUT_SHA256),
}
SUITE_ARGS = ('-m', 'unittest', 'discover', '-s', 'tests', '-v')
HASHED_PATTERNS = ('*.py', 'tests/*.py', '*.log', '*.md')
# Rewritten by every run of their gate (unittest prints the run duration), so a receipt may
# only hash its own copy; another gate's copy would go stale the moment that gate runs again.
REGENERATED_OUTPUTS = ('*-verify-suite.log',)


class VerificationError(RuntimeError):
    """A verify-script check failed; no receipt is written."""


def check(condition, message):
    if not condition:
        raise VerificationError(message)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_inputs():
    """Read the four supplied inputs from data/, each bound to its recorded hash; return them plus the archive hash."""
    raw = {}
    for name, expected in INPUT_SHA256.items():
        path = DATA / name
        check(path.is_file(), f'supplied input {path} is missing')
        data = path.read_bytes()
        digest = sha256(data)
        check(digest == expected, f'{name}: hash {digest} differs from the recorded {expected}')
        raw[name] = data
    return raw, ARCHIVE_SHA256


def run_suite(root: Path, tz: str | None = None) -> dict:
    """Run the unit suite with this interpreter and return only measured values."""
    env = dict(os.environ)
    if tz is not None:
        env['TZ'] = tz
    command = [sys.executable, *SUITE_ARGS]
    run = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True)
    output = run.stderr + run.stdout
    match = re.search(r'^Ran (\d+) tests?', output, re.M)
    check(match is not None, f'unittest summary missing (TZ={tz}):\n{output[-2000:]}')
    check(run.returncode == 0, f'test suite exit {run.returncode} (TZ={tz}):\n{output[-4000:]}')
    summary = [line for line in output.splitlines() if line.strip()][-1]
    check(summary == 'OK', f'unexpected unittest summary {summary!r} (TZ={tz})')
    return {'command': ' '.join(command), 'tz': tz if tz is not None else 'inherited',
            'exit_code': run.returncode, 'tests_ran': int(match.group(1)), 'summary': summary, 'output': output}


def loaded_test_count(root: Path) -> int:
    return unittest.defaultTestLoader.discover(str(root / 'tests')).countTestCases()


def verified_suite(root: Path, zones=(None,)) -> tuple[dict, int, str]:
    """Run the suite per zone; the test count must agree across runs and with the loader."""
    runs = {}
    transcript = []
    for tz in zones:
        run = run_suite(root, tz)
        transcript.append(f'### TZ={run["tz"]} :: {run["command"]} :: exit {run["exit_code"]}\n{run.pop("output")}')
        runs[run['tz']] = run
    counts = {run['tests_ran'] for run in runs.values()}
    check(len(counts) == 1, f'test counts differ across runs: {counts}')
    tests = counts.pop()
    loaded = loaded_test_count(root)
    check(loaded == tests, f'loader counts {loaded} tests but the run reported {tests}')
    return runs, tests, '\n'.join(transcript)


def _regenerated_outputs(root: Path) -> set[str]:
    return {path.relative_to(root).as_posix() for pattern in REGENERATED_OUTPUTS for path in root.glob(pattern)}


def artifact_hashes(root: Path, own_outputs=()) -> dict:
    """Hash sources, tests, documents, preserved logs and this gate's own regenerated outputs only."""
    names = sorted({path.relative_to(root).as_posix() for pattern in HASHED_PATTERNS for path in root.glob(pattern)})
    excluded = _regenerated_outputs(root) - set(own_outputs)
    return {name: sha256((root / name).read_bytes()) for name in names if name not in excluded}


def hash_scope(root: Path, own_outputs=()) -> dict:
    """Receipt-visible statement of what artifact_hashes covered and what it left out, and why."""
    return {'patterns': list(HASHED_PATTERNS), 'own_regenerated_outputs': sorted(own_outputs),
            'excluded_regenerated_outputs': sorted(_regenerated_outputs(root) - set(own_outputs)),
            'reason': 'other gates rewrite their suite logs on every run; hashing them would make this receipt stale'}


def historical_red(root: Path, name: str) -> dict:
    """Describe a preserved red log without inventing an exit code for it."""
    path = root / name
    if not path.exists():
        return {'log': name, 'present': False, 'exit_code': 'unknown'}
    lines = [line for line in path.read_text(errors='replace').splitlines() if line.strip()]
    return {'log': name, 'present': True, 'exit_code': 'unknown', 'log_summary': lines[-1] if lines else '',
            'note': 'red log preserved from the first implementation pass; this script did not observe that run'}


def interpreter() -> dict:
    return {'executable': sys.executable, 'version': platform.python_version(),
            'implementation': platform.python_implementation()}


def write_receipt(root: Path, name: str, receipt: dict) -> None:
    (root / name).write_text(json.dumps(receipt, indent=2, sort_keys=True) + '\n')
