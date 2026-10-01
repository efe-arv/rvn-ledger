"""Measure the installed batch CLI on reproducible synthetic inputs; opt-in only."""
import argparse
import contextlib
import hashlib
from importlib.metadata import version
import io
import json
import os
from pathlib import Path
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import tracemalloc

ROOT = Path(__file__).resolve().parents[1]


def source_digest():
    """Bind results to the installed Python sources, including uncommitted candidates."""
    import rvn_ledger
    source = Path(rvn_ledger.__file__).parent
    digest = hashlib.sha256()
    for path in sorted(source.glob('*.py')):
        digest.update(path.name.encode() + b'\0' + path.read_bytes() + b'\0')
    return digest.hexdigest()


def generate_inputs(directory, count):
    """All-valid unique events retain a source reference for every input row."""
    directory.mkdir()
    for name in ('accounts.json', 'plans.json', 'period.json'):
        shutil.copyfile(ROOT / 'examples/demo' / name, directory / name)
    with (directory / 'events.jsonl').open('w', encoding='utf-8', newline='\n') as handle:
        for i in range(count):
            event = {'event_id': f'benchmark-{i:09d}', 'ingest_seq': i,
                     'account_id': 'demo-try' if i % 2 else 'demo-usd',
                     'metric': 'api_calls' if i % 3 else 'storage_gb_hours',
                     'units': i % 17 + 1, 'ts': '2026-09-20T12:00:00Z', 'ingested_at': '2026-09-20T12:01:00Z'}
            handle.write(json.dumps(event, separators=(',', ':')) + '\n')


def worker(data, out):
    """Measure read -> bill -> reconcile -> publish -> checked read in a fresh process."""
    from rvn_ledger.cli import main as ledger_main
    from rvn_ledger.pipeline import tzdata_version
    captured = io.StringIO()
    tracemalloc.start()
    started = time.perf_counter()
    with contextlib.redirect_stdout(captured):
        code = ledger_main(['run', '--input-dir', str(data), '--out', str(out), '--json'])
    seconds = time.perf_counter() - started
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    if code:
        raise RuntimeError(f'billing exited with {code}')
    counts = json.loads(captured.getvalue())['counts']
    if counts['accepted'] != counts['raw']:
        raise RuntimeError('benchmark fixture did not accept every event')
    return {'seconds': round(seconds, 6), 'peak_python_mib': round(peak / 1024**2, 3),
            'events': counts['raw'], 'output_bytes': sum(p.stat().st_size for p in out.iterdir()),
            'tzdata': tzdata_version(), 'source_sha256': source_digest()}


def measure(args):
    results = []
    with tempfile.TemporaryDirectory(prefix='rvn-ledger-benchmark-') as tmp:
        root = Path(tmp)
        for scale in args.scales:
            data = root / f'inputs-{scale}'
            generate_inputs(data, args.base_events * scale)
            samples = []
            for repeat in range(args.repeats):
                command = [sys.executable, '-B', str(Path(__file__).resolve()), '--worker',
                           '--input-dir', str(data), '--out-dir', str(root / f'out-{scale}-{repeat}')]
                run = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', timeout=600,
                                     env={**os.environ, 'PYTHONIOENCODING': 'utf-8', 'PYTHONDONTWRITEBYTECODE': '1'})
                if run.returncode:
                    raise RuntimeError(run.stderr)
                samples.append(json.loads(run.stdout))
                print(f'{scale}x sample {repeat + 1}/{args.repeats}: {samples[-1]["seconds"]:.3f}s', flush=True)
            results.append({'scale': scale, 'events': args.base_events * scale,
                            'median_seconds': round(statistics.median(s['seconds'] for s in samples), 3),
                            'max_peak_python_mib': max(s['peak_python_mib'] for s in samples), 'samples': samples})
    return {'version': version('rvn-ledger'), 'python': platform.python_version(), 'platform': platform.platform(),
            'base_events': args.base_events, 'repeats': args.repeats,
            'fixture': 'synthetic unique accepted events; two accounts/currencies and two metrics',
            'measurement': 'fresh process per sample; CLI run including input I/O, publication and check; tracing enabled',
            'memory': 'peak traced Python allocations; excludes interpreter startup, native/OS memory and filesystem cache',
            'limitations': 'local timings with tracemalloc overhead; no production SLA or unmeasured 1000x claim',
            'results': results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scales', type=int, nargs='+', default=[1, 10, 100])
    parser.add_argument('--base-events', type=int, default=1000)
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--json', type=Path, default=Path('benchmark-results.json'))
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--input-dir', type=Path, help=argparse.SUPPRESS)
    parser.add_argument('--out-dir', type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        print(json.dumps(worker(args.input_dir, args.out_dir)))
        return
    if min([args.base_events, args.repeats, *args.scales]) < 1:
        parser.error('counts, scales and repeats must be positive')
    result = measure(args)
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    print(f'Saved {args.json}')


if __name__ == '__main__':
    main()
