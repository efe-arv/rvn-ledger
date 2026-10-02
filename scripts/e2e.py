"""End-to-end check of the INSTALLED console command against the hand-derived demo outputs.

Run from a non-editable install: run, check, rerun (byte-identical), explain, and tamper detection.
"""
import argparse
import hashlib
from importlib.metadata import version
from importlib.util import find_spec
import json
import os
from pathlib import Path
import shutil
import subprocess
import sysconfig
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


def exercise(cli, directory):
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
    account = invoke('explain', '--out', out, '--account', 'demo-try')
    expected_invoice = next(i for i in json.loads((demo / 'expected-invoices.json').read_bytes()) if i['account_id'] == 'demo-try')
    require(account['invoice'] == expected_invoice and sum(s['units'] for s in account['usage_sources']['api_calls']) == 12,
            'account explanation differs from the hand-derived invoice')

    # A UTF-8 BOM in front of the demo must not hand `api-first` to its conflicting later copy; hashes cover the raw bytes.
    marked, marked_out = directory / 'inputs-bom', directory / 'out-bom'
    shutil.copytree(data, marked)
    raw = (data / 'events.jsonl').read_bytes()
    (marked / 'events.jsonl').write_bytes(b'\xef\xbb\xbf' + raw)
    invoke('run', '--input-dir', marked, '--out', marked_out)
    for name in ('invoices', 'quarantine'):
        expected = json.loads((demo / f'expected-{name}.json').read_bytes())
        require(json.loads((marked_out / f'{name}.json').read_bytes()) == expected, f'{name} differs from hand-derived fixture with a leading BOM')
    manifest = json.loads((marked_out / 'manifest.json').read_bytes())
    require(manifest['inputs']['events.jsonl']['sha256'] == hashlib.sha256(b'\xef\xbb\xbf' + raw).hexdigest(), 'BOM input hash is not of the raw bytes')
    copies = invoke('explain', '--out', marked_out, '--event-id', 'api-first', '--events', marked / 'events.jsonl')['records']
    require(len(copies) == 2 and copies[0]['status'] == 'accepted' and copies[1]['canonical_line'] == 1, 'BOM changed canonical selection')

    missing = invoke('explain', '--out', out, '--events', directory / 'absent.jsonl', expected=2)
    require(missing.stderr.startswith('error: --events: cannot read'), f'unexpected explain diagnostic: {missing.stderr}')
    (out / 'invoices.json').write_bytes(b'[]\n')
    invoke('check', '--out', out, expected=2)
    tampered = invoke('explain', '--out', out, expected=2)
    require(tampered.stderr.startswith('check failed:'), f'unexpected explain diagnostic: {tampered.stderr}')
    return {'version': version('rvn-ledger'), 'calls': calls, 'output_sha256': original, 'result': 'OK'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cli', type=Path, default=Path(sysconfig.get_path('scripts')) / ('rvn-ledger.exe' if os.name == 'nt' else 'rvn-ledger'))
    args = parser.parse_args()
    location = Path(find_spec('rvn_ledger').origin).resolve()
    require(not location.is_relative_to(ROOT / 'src'), 'install the package non-editably before E2E')
    with tempfile.TemporaryDirectory(prefix='rvn-ledger-e2e-') as tmp:
        result = exercise(args.cli.resolve(), Path(tmp))
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
