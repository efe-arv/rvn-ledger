"""Ledger command line: read the four inputs, write invoices.json, quarantine.json, audit.json and manifest.json.

    rvn-ledger run --input-dir DIR --out OUT
    rvn-ledger run --events E --accounts A --plans P --period R --out OUT
    rvn-ledger check --out OUT
    rvn-ledger explain --out OUT [--event-id ID | --line N | --account ID] [--events events.jsonl]
    rvn-ledger export --out OUT --file report.xlsx            (needs the [excel] extra)
    rvn-ledger import --file inputs.xlsx --input-dir NEW_DIR  (needs the [excel] extra)

(`python -m rvn_ledger ...` is equivalent.)

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


def display(message, stream):
    """Preserve representable text; escape only unsupported characters for human display."""
    encoding = getattr(stream, 'encoding', None)
    text = str(message)
    return text.encode(encoding, errors='backslashreplace').decode(encoding) if encoding else text


def human_display(message, *, file=None):
    stream = sys.stdout if file is None else file
    print(display(message, stream), file=stream)


def parse_args(argv):
    parser = argparse.ArgumentParser(prog='rvn-ledger', description='Turn a month of raw usage events into exact, reproducible invoices.')
    from . import __version__
    parser.add_argument('--version', action='version', version=f'rvn-ledger {__version__}')
    commands = parser.add_subparsers(dest='command', required=True)
    run = commands.add_parser('run', help='produce the output set from the four inputs')
    run.add_argument('--input-dir', type=Path, default=Path('data'), help='directory holding events.jsonl, accounts.json, plans.json and period.json')
    for name in INPUT_NAMES:
        run.add_argument(f'--{name.split(".")[0]}', type=Path, help=f'path to {name} (overrides --input-dir)')
    run.add_argument('--out', type=Path, default=Path("out"), help='output directory (created; staged replacement with rollback on caught errors)')
    check = commands.add_parser('check', help='verify a published output set: manifest hashes and audit reconciliation')
    check.add_argument('--out', type=Path, default=Path("out"))
    diagnostic = commands.add_parser('explain', help="explain quarantined records, an exact event/source line, or one account's invoice")
    diagnostic.add_argument('--out', type=Path, default=Path('out'))
    selection = diagnostic.add_mutually_exclusive_group()
    selection.add_argument('--event-id', help='exact event identifier; includes its duplicate copies')
    selection.add_argument('--line', type=int, help='physical events.jsonl line number (1-based)')
    selection.add_argument('--account', help="account id: its invoice, every line's formula, the source events and the credit")
    diagnostic.add_argument('--events', type=Path, help='optional original events.jsonl; values shown only after hash match')
    export = commands.add_parser('export', help='export a checked report or lossless input workbook to Excel')
    export.add_argument('--format', choices=['xlsx'], default='xlsx')
    export.add_argument('--kind', choices=['report', 'inputs'], default='report', help='report cannot be imported; inputs uses the explicit v1 transport schema')
    export.add_argument('--out', type=Path, default=Path('out'), help='checked output set for report export')
    export.add_argument('--input-dir', type=Path, default=Path('data'), help='four input files for --kind inputs')
    export.add_argument('--file', type=Path, required=True, help='new XLSX file; existing files are never overwritten')
    importer = commands.add_parser('import', help='validate an input workbook and write the four inputs to a NEW directory')
    importer.add_argument('--file', type=Path, required=True)
    importer.add_argument('--input-dir', type=Path, required=True, help='NEW destination directory; existing directories are never overwritten')
    for command in (run, check, diagnostic, export, importer):
        command.add_argument('--json', action='store_true', help='machine-readable summary')
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
        human_display(f"OK: {manifest['counts']['invoices']} invoices; {manifest['counts']['quarantine_entries']} quarantined -> {args.out}")
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
    return _summary(args, summary, f'OK: output hashes and audit reconciled -> {args.out}')


def _summary(args, summary, message):
    if args.json:
        print(json.dumps(summary, indent=2, ensure_ascii=True))
    else:
        human_display(message)
    return 0


def _account_lines(r):
    inv = r['invoice']
    lines = [f"{r['account_id']}  {r['currency']} (amounts in minor units)  {r['timezone']}  period-end plan {r['period_end_plan_id']}"]
    for s in r['subscription_lines']:
        lines.append(f"  subscription {s['plan_id']} {s['from']}..{s['to']} {s['days']} days: {s['formula']}")
    for u in r['usage_lines']:
        upper = 'open' if u['tier_to'] is None else u['tier_to']
        lines.append(f"  usage {u['metric']} [{u['tier_from']}, {upper}) {u['units']} units: {u['formula']}")
    lines.append(f"  subtotal {inv['subtotal_minor']}  credit {r['credit']['credit_minor']} (applied {inv['credit_applied_minor']}, "
                 f"remaining {inv['credit_remaining_minor']})  total {inv['total_minor']}")
    for metric, units in inv['billable_units'].items():
        rows = r['usage_sources'][metric]
        lines.append(f"  sources {metric}: {len(rows)} event{'' if len(rows) == 1 else 's'}, {units} units" +
                     ''.join(f"\n    line {s['line']} {json.dumps(s['event_id'], ensure_ascii=True)} {s['units']}" for s in rows))
    quarantined = r['quarantined_lines']
    lines.append(f"  quarantined lines: {', '.join(map(str, quarantined)) if quarantined else 'none'}")
    return lines


def command_explain(args):
    from .diagnostics import explain, explain_account, load_verified
    data = load_verified(args.out)
    if args.account is not None:
        if args.events is not None:
            raise InputError('--events applies to the quarantine list, --event-id and --line, not --account')
        result = explain_account(data, args.account)
        return _summary(args, result, '\n'.join(_account_lines(result)))
    result = explain(data, event_id=args.event_id, line=args.line, events=args.events)
    # ASCII escapes keep identifiers safe on Windows legacy codepages.
    lines = [f"{r['source']['file']}:{r['source']['line']} event={json.dumps(r['event_id'], ensure_ascii=True)} status={r['status']}" +
             ''.join(f"\n  {reason['code']} ({reason['field']}): {reason['message']}" +
                     (f" value={json.dumps(reason['value'], ensure_ascii=True)}" if 'value' in reason else '') for reason in r['reasons']) +
             (f"\n  canonical_line={r['canonical_line']}" if 'canonical_line' in r else '') for r in result['records']]
    return _summary(args, result, '\n'.join(lines) if lines else 'OK: no quarantined records')


def _excel_support():
    try:
        from . import excel
    except ModuleNotFoundError as exc:
        if exc.name.split('.')[0] not in ('openpyxl', 'defusedxml', 'et_xmlfile'):
            raise
        raise InputError('Excel support is optional; install rvn-ledger[excel]; see docs/EXCEL.md') from exc
    return excel


def command_export(args):
    excel = _excel_support()
    from .diagnostics import load_verified
    if args.kind == 'inputs':
        raw = {name: (args.input_dir / name).read_bytes() for name in INPUT_NAMES}
        # Inputs intended for editing may contain bad events, but configuration must be usable.
        result = run_ledger(raw)
        reconcile(result.invoices, result.quarantine, result.audit, result.manifest)
        excel.export_inputs(raw, args.file)
    else:
        excel.export_report(load_verified(args.out), args.file)
    return _summary(args, {'file': str(args.file), 'kind': args.kind, 'result': 'OK'}, f'OK: {args.kind} workbook -> {args.file}')


def command_import(args):
    import os
    import shutil
    import tempfile
    raw = _excel_support().import_inputs(args.file)
    result = run_ledger(raw)
    reconcile(result.invoices, result.quarantine, result.audit, result.manifest)
    target = args.input_dir
    if target.exists() or target.is_symlink():
        raise InputError('import destination exists; choose a NEW --input-dir')
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.ledger-import-', dir=target.parent))
    acquired = False
    try:
        for name, data in raw.items():
            with open(staging / name, 'xb') as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        target.mkdir()  # Exclusive claim: never replace another directory, even after a race.
        acquired = True
        for name in INPUT_NAMES:
            os.replace(staging / name, target / name)
    except BaseException:
        if acquired:
            shutil.rmtree(target)
        raise
    finally:
        shutil.rmtree(staging)
    return _summary(args, {'input_dir': str(target), 'result': 'OK', 'counts': result.manifest['counts']},
                    f"OK: input workbook validated; {result.manifest['counts']['quarantine_entries']} quarantined events retained -> {target}")


def main(argv=None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        return {'run': command_run, 'check': command_check, 'explain': command_explain,
                'export': command_export, 'import': command_import}[args.command](args)
    except InputError as exc:
        human_display(f'error: {exc}', file=sys.stderr)
        return 2
    except (OutputError, OSError) as exc:
        # Only `run` publishes; `check` and `explain` read a published set, so their failures are check failures.
        human_display(f'{"publication failed" if args.command == "run" else "check failed"}: {exc}', file=sys.stderr)
        return 2
    except AuditError as exc:
        if args.command in ('check', 'explain') or (args.command == 'export' and args.kind == 'report'):
            human_display(f'check failed: {exc}', file=sys.stderr)
            return 2
        human_display(f'internal error: run did not reconcile, nothing published: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
