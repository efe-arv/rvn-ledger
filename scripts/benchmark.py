"""Measure the installed batch CLI on reproducible synthetic inputs; opt-in only.

Each scale is run untraced for wall time (median of --repeats fresh processes) and once more
under tracemalloc for peak Python allocations, so tracing overhead does not inflate the timing.
"""
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


def worker(data, out, trace):
    """Measure read -> bill -> reconcile -> publish -> checked read in a fresh process."""
    from rvn_ledger.cli import main as ledger_main
    captured = io.StringIO()
    if trace:
        tracemalloc.start()
    started = time.perf_counter()
    with contextlib.redirect_stdout(captured):
        code = ledger_main(['run', '--input-dir', str(data), '--out', str(out), '--json'])
    seconds = time.perf_counter() - started
    peak = tracemalloc.get_traced_memory()[1] if trace else None
    if trace:
        tracemalloc.stop()
    if code:
        raise RuntimeError(f'billing exited with {code}')
    counts = json.loads(captured.getvalue())['counts']
    if counts['accepted'] != counts['raw']:
        raise RuntimeError('benchmark fixture did not accept every event')
    return {'seconds': round(seconds, 6), 'peak_python_mib': round(peak / 1024**2, 3) if trace else None,
            'events': counts['raw'], 'output_bytes': sum(p.stat().st_size for p in out.iterdir()),
            'tzdata': json.loads((out / 'manifest.json').read_bytes())['versions']['tzdata'], 'source_sha256': source_digest()}


def measure(args):
    results = []
    with tempfile.TemporaryDirectory(prefix='rvn-ledger-benchmark-') as tmp:
        root = Path(tmp)
        for scale in args.scales:
            data = root / f'inputs-{scale}'
            generate_inputs(data, args.base_events * scale)
            samples = []
            for repeat in range(args.repeats + 1):
                trace = repeat == args.repeats            # last run: memory only
                command = [sys.executable, '-B', str(Path(__file__).resolve()), '--worker',
                           '--input-dir', str(data), '--out-dir', str(root / f'out-{scale}-{repeat}'), *(['--trace'] if trace else [])]
                run = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', timeout=3600,
                                     env={**os.environ, 'PYTHONIOENCODING': 'utf-8', 'PYTHONDONTWRITEBYTECODE': '1'})
                if run.returncode:
                    raise RuntimeError(run.stderr)
                samples.append(json.loads(run.stdout))
                shutil.rmtree(root / f'out-{scale}-{repeat}', ignore_errors=True)
                print(f'{scale}x {"traced" if trace else f"sample {repeat + 1}/{args.repeats}"}: {samples[-1]["seconds"]:.3f}s', flush=True)
            timed = [s for s in samples if s['peak_python_mib'] is None]
            traced = samples[-1]
            results.append({'scale': scale, 'events': args.base_events * scale,
                            'median_seconds': round(statistics.median(s['seconds'] for s in timed), 3),
                            'per_event_ms': round(1000 * statistics.median(s['seconds'] for s in timed) / (args.base_events * scale), 4),
                            'peak_python_mib': traced['peak_python_mib'], 'traced_seconds': traced['seconds'],
                            'output_mib': round(traced['output_bytes'] / 1024**2, 1), 'samples': samples})
            shutil.rmtree(data, ignore_errors=True)
    return {'version': version('rvn-ledger'), 'python': platform.python_version(), 'platform': platform.platform(),
            'base_events': args.base_events, 'repeats': args.repeats,
            'fixture': 'synthetic unique accepted events; two accounts/currencies and two metrics',
            'measurement': 'fresh process per sample; CLI run including input I/O, reconciliation, publication and check; '
                           'median_seconds is untraced, peak_python_mib comes from one extra traced run',
            'memory': 'peak traced Python allocations; excludes interpreter startup, native/OS memory and filesystem cache',
            'limitations': 'one machine, host load and file cache not controlled; not a latency guarantee',
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
    parser.add_argument('--trace', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        print(json.dumps(worker(args.input_dir, args.out_dir, args.trace)))
        return
    if min([args.base_events, args.repeats, *args.scales]) < 1:
        parser.error('counts, scales and repeats must be positive')
    result = measure(args)
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    print(f'Saved {args.json}')


if __name__ == '__main__':
    main()
