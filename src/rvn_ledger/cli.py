"""Ledger command line: read the four inputs, write invoices.json, quarantine.json, audit.json and manifest.json.

    python3 cli.py run --input-dir DIR --out OUT
    python3 cli.py run --events E --accounts A --plans P --period R --out OUT
    python3 cli.py check --out OUT

Exit codes: 0 success; 2 usage error, unusable input/configuration, publication/I/O failure, or a published set
that fails its check; 1 an internal inconsistency (the run did not reconcile with its
own audit trail; nothing was published). Standard-library billing, local IANA data, no network or model.
"""
import argparse
import json
import sys
from pathlib import Path



from .audit import AuditError, reconcile  # noqa: E402
from .inputs import InputError  # noqa: E402
from .outputs import OutputError, check_outputs, publish, serialize  # noqa: E402
from .pipeline import INPUT_NAMES, run_ledger  # noqa: E402


def parse_args(argv):
    parser = argparse.ArgumentParser(prog='rvn-ledger', description='Turn a month of raw usage events into exact, reproducible invoices.')
    parser.add_argument('--version', action='version', version='rvn-ledger 1.0.1')
    commands = parser.add_subparsers(dest='command', required=True)
    run = commands.add_parser('run', help='produce the output set from the four inputs')
    run.add_argument('--input-dir', type=Path, default=Path('data'), help='directory holding events.jsonl, accounts.json, plans.json and period.json')
    for name in INPUT_NAMES:
        run.add_argument(f'--{name.split(".")[0]}', type=Path, help=f'path to {name} (overrides --input-dir)')
    run.add_argument('--out', type=Path, default=Path("out"), help='output directory (created; staged replacement with rollback on caught errors)')
    check = commands.add_parser('check', help='verify a published output set: manifest hashes and audit reconciliation')
    check.add_argument('--out', type=Path, default=Path("out"))
    run.add_argument('--json', action='store_true', help='machine-readable summary')
    check.add_argument('--json', action='store_true', help='machine-readable summary')
    argv = [{' -help': '--help', '-help': '--help', '-run': 'run', '-check': 'check'}.get(a, a) for a in argv]
    return parser.parse_args(argv)


def read_inputs(args) -> dict:
    raw = {}
    for name in INPUT_NAMES:
        explicit = getattr(args, name.split('.')[0])
        path = explicit if explicit is not None else (args.input_dir / name if args.input_dir is not None else None)
        if path is None:
            raise InputError(f'{name}: give --input-dir or --{name.split(".")[0]}')
        try:
            raw[name] = Path(path).read_bytes()
        except OSError as exc:
            raise InputError(f'{name}: cannot read {path}: {exc.strerror}') from exc
    return raw


def command_run(args) -> int:
    run = run_ledger(read_inputs(args))
    reconcile(run.invoices, run.quarantine, run.audit, run.manifest)      # invariants before anything is written
    files = {'invoices.json': serialize(run.invoices), 'quarantine.json': serialize(run.quarantine), 'audit.json': serialize(run.audit)}
    manifest = publish(args.out, files, run.manifest)
    import contextlib
    import io
    with contextlib.redirect_stdout(io.StringIO()):
        command_check(args)
    summary = {'out': str(args.out), 'counts': manifest['counts'], 'totals_by_currency': manifest['totals_by_currency']}
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(f"OK: {manifest['counts']['invoices']} invoices; {manifest['counts']['quarantine_entries']} quarantined -> {args.out}")
    return 0


def command_check(args) -> int:
    manifest = check_outputs(args.out)
    def load(name):
        try:
            return json.loads((args.out / name).read_bytes().decode('utf-8'))
        except (OSError, UnicodeDecodeError, ValueError, RecursionError) as exc:
            raise OutputError(f'{name}: unreadable or invalid JSON') from exc
    reconcile(load('invoices.json'), load('quarantine.json'), load('audit.json'), manifest)
    summary = {'out': str(args.out), 'result': 'OK', 'counts': manifest['counts']}
    print(json.dumps(summary, indent=2) if args.json else f'OK: output hashes and audit reconciled -> {args.out}')
    return 0


def main(argv=None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        return command_run(args) if args.command == 'run' else command_check(args)
    except InputError as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 2
    except (OutputError, OSError) as exc:
        print(f'{"check failed" if args.command == "check" else "publication failed"}: {exc}', file=sys.stderr)
        return 2
    except AuditError as exc:
        if args.command == 'check':
            print(f'check failed: {exc}', file=sys.stderr)
            return 2
        print(f'internal error: run did not reconcile, nothing published: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
