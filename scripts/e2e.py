"""Exercise an installed console command against hand-derived synthetic outputs."""
import argparse
import hashlib
from importlib.metadata import version
from importlib.util import find_spec
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
INPUTS = ('events.jsonl', 'accounts.json', 'plans.json', 'period.json')
OUTPUTS = ('invoices.json', 'quarantine.json', 'audit.json', 'manifest.json')


def require(condition, message):
    """Keep E2E checks active under optimized Python as well."""
    if not condition:
        raise RuntimeError(message)


def snapshot(directory):
    return {name: hashlib.sha256((directory / name).read_bytes()).hexdigest() for name in OUTPUTS}


def exercise(cli, directory, core_only):
    """Run the whole user workflow from outside the checkout."""
    calls = []

    def invoke(*args, expected=0, machine=True):
        command = [str(cli), *map(str, args), *(['--json'] if machine else [])]
        result = subprocess.run(command, cwd=directory, capture_output=True, text=True,
                                encoding='utf-8', timeout=60,
                                env={**os.environ, 'PYTHONIOENCODING': 'utf-8', 'PYTHONDONTWRITEBYTECODE': '1'})
        require(result.returncode == expected and 'Traceback' not in result.stderr,
                f'{args}: exit {result.returncode}, stderr={result.stderr}')
        calls.append({'command': str(args[0]), 'exit': result.returncode})
        return json.loads(result.stdout) if machine and expected == 0 else result

    data, out = directory / 'inputs', directory / 'out'
    data.mkdir()
    demo = ROOT / 'examples' / 'demo'
    for name in INPUTS:
        shutil.copyfile(demo / name, data / name)
    invoke('--help', machine=False)
    shown = invoke('--version', machine=False).stdout.strip()
    require(shown == f'rvn-ledger {version("rvn-ledger")}', 'console/package version mismatch')
    invoke('run', '--input-dir', data, '--out', out)
    invoke('check', '--out', out)
    for name in ('invoices', 'quarantine'):
        expected = json.loads((demo / f'expected-{name}.json').read_bytes())
        require(json.loads((out / f'{name}.json').read_bytes()) == expected, f'{name} differs from hand-derived fixture')
    original = snapshot(out)
    invoke('run', '--input-dir', data, '--out', out)
    require(snapshot(out) == original, 'repeated output bytes differ')
    records = invoke('explain', '--out', out, '--events', data / 'events.jsonl')
    require(records['count'] == 3 and records['source_values_verified'], 'quarantine explanations differ')
    copies = invoke('explain', '--out', out, '--event-id', 'api-first')['records']
    require(len(copies) == 2 and copies[1]['canonical_line'] == 1, 'global canonical selection changed')

    report, workbook = directory / 'report.xlsx', directory / 'inputs.xlsx'
    if core_only:
        require(find_spec('openpyxl') is None and find_spec('defusedxml') is None,
                'core-only E2E requires an environment without the Excel extra')
        rejected = invoke('export', '--out', out, '--file', report, expected=2)
        require('rvn-ledger[excel]' in rejected.stderr and not report.exists(), 'missing-extra error is not actionable')
        rejected = invoke('import', '--file', workbook, '--input-dir', directory / 'unavailable', expected=2)
        require('rvn-ledger[excel]' in rejected.stderr, 'import does not explain the missing extra')
    else:
        invoke('export', '--out', out, '--file', report)
        invoke('export', '--kind', 'inputs', '--input-dir', data, '--file', workbook)
        imported = directory / 'imported'
        invoke('import', '--file', workbook, '--input-dir', imported)
        require(all((data / name).read_bytes() == (imported / name).read_bytes() for name in INPUTS),
                'Excel transport changed input bytes')
        other = directory / 'imported-out'
        invoke('run', '--input-dir', imported, '--out', other)
        invoke('check', '--out', other)
        require(snapshot(other) == original, 'Excel transport changed billing outputs')
        report_hash = hashlib.sha256(report.read_bytes()).hexdigest()
        invoke('export', '--out', out, '--file', report, expected=2)
        require(hashlib.sha256(report.read_bytes()).hexdigest() == report_hash, 'existing report was overwritten')
        invoke('import', '--file', report, '--input-dir', directory / 'invalid', expected=2)
        require(not (directory / 'invalid').exists(), 'rejected report created an import destination')
    (out / 'invoices.json').write_bytes(b'[]\n')
    invoke('check', '--out', out, expected=2)
    invoke('explain', '--out', out, expected=2)
    return {'version': version('rvn-ledger'), 'mode': 'core' if core_only else 'excel',
            'calls': calls, 'output_sha256': original, 'result': 'OK'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--core-only', action='store_true')
    parser.add_argument('--cli', type=Path, default=Path(sys.executable).parent / ('rvn-ledger.exe' if os.name == 'nt' else 'rvn-ledger'))
    args = parser.parse_args()
    location = Path(find_spec('rvn_ledger').origin).resolve()
    require(not location.is_relative_to(ROOT / 'src'), 'install the package non-editably before E2E')
    with tempfile.TemporaryDirectory(prefix='rvn-ledger-e2e-') as tmp:
        result = exercise(args.cli.resolve(), Path(tmp), args.core_only)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
