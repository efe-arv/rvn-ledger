"""End-to-end gate on the supplied inputs: every invoice recomputed independently, outputs byte-identical across clean runs.

Independence: the accepted/quarantined set comes from `verify_timing.independent_statuses` (raw-byte re-derivation
without the product modules), accepted payloads are re-read with the plain json module, subscription days are counted
by walking each local date against the raw plan segments, tiers are split with a private bracket walk, every amount
is recomputed with exact `Fraction` half-up rounding, and the expected contract document is built here and compared
whole against invoices.json. The product's own `audit.reconcile` is run as well, but only after the independent check.

Clean runs: the CLI is executed as a subprocess from two different working directories, under a different TZ, with a
minimal environment, with `python -I` (no user site; installed tzdata remains available), and, when the host
allows it, inside `unshare -rn` (no network namespace). Every run must produce byte-identical files.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path
from rvn_ledger.audit import reconcile
from rvn_ledger.inputs import InputError
from verify_support import (SOURCE_PROVENANCE, VerificationError, artifact_hashes, check, hash_scope, historical_red,
                            interpreter, load_inputs, sha256, verified_suite, write_receipt)
from verify_timing import independent_statuses, instant

ROOT = Path(__file__).resolve().parent
ZONES = ('UTC', 'Pacific/Honolulu')
OWN_OUTPUTS = ('invoices-verify-suite.log',)
OUTPUT_FILES = ('invoices.json', 'quarantine.json', 'audit.json', 'manifest.json')
FORBIDDEN_MANIFEST_WORDS = ('timestamp', 'generated_at', 'run_id', 'uuid', 'created', str(ROOT))


def half_up(numerator: int, denominator: int) -> int:
    return int(Fraction(numerator, denominator) + Fraction(1, 2))


def bracket_split(units: int, tiers: list) -> list:
    """Private cumulative bracket walk: [(from, to, units_in_bracket, price), ...] for brackets that carry units."""
    out = []
    for tier in tiers:
        lower, upper = tier['from_units'], tier['to_units']
        if units <= lower:
            break
        take = (units if upper is None else min(units, upper)) - lower
        if take > 0:
            out.append((lower, upper, take, tier['unit_price_micros']))
    return out


def primary_reason(fields: dict, known_accounts: set, metrics: set) -> str:
    """Documented precedence: account, metric, units, usage timestamp, ingestion timestamp."""
    if not isinstance(fields.get('account_id'), str) or fields['account_id'] not in known_accounts:
        return 'unknown_account'
    if not isinstance(fields.get('metric'), str) or fields['metric'] not in metrics:
        return 'unknown_metric'
    units = fields.get('units')
    if type(units) is not int:
        return 'invalid_units'
    if units <= 0:
        return 'nonpositive_units'
    if instant(fields.get('ts')) is None:
        return 'invalid_ts'
    if instant(fields.get('ingested_at')) is None:
        return 'invalid_ingested_at'
    return 'invalid_json'   # strictly rejected line whose identity was salvaged


def expected_documents(raw: dict, accounts: list, period: dict, plans: dict) -> tuple[list, list, Counter]:
    statuses = independent_statuses(raw['events.jsonl'], accounts, period)
    lines = raw['events.jsonl'].split(b'\n')
    if lines and lines[-1] == b'':
        lines.pop()
    known = {a['account_id'] for a in accounts}
    metrics = list(period['metrics'])
    usage = {a: {m: 0 for m in metrics} for a in known}
    quarantined_by_account = Counter()
    quarantine = []
    for number, line in enumerate(lines, 1):
        status = statuses[number]
        if status not in ('accepted', 'quarantined'):
            continue
        try:
            fields = json.loads(line.decode('utf-8'))
        except (UnicodeDecodeError, ValueError, RecursionError):
            fields = None
        if status == 'accepted':
            usage[fields['account_id']][fields['metric']] += fields['units']
            continue
        if isinstance(fields, dict):
            event_id = fields.get('event_id') if isinstance(fields.get('event_id'), str) and fields.get('event_id') else None
            reason = primary_reason(fields, known, set(metrics))
            if isinstance(fields.get('account_id'), str) and fields['account_id'] in known:
                quarantined_by_account[fields['account_id']] += 1
        else:
            event_id, reason = None, None   # unreadable line: reason not re-derived here, only presence is checked
        quarantine.append({'event_id': event_id, 'reason': reason})

    start = date.fromisoformat(period['period_start_local'])
    end = date.fromisoformat(period['period_end_local_exclusive'])
    period_dates = [start + timedelta(days=i) for i in range((end - start).days)]
    invoices = []
    for account in sorted(accounts, key=lambda a: a['account_id']):
        currency = account['currency']
        document_lines, end_plan = [], None
        for seg in sorted(account['plan_segments'], key=lambda s: s['from']):
            covered = [d for d in period_dates if date.fromisoformat(seg['from']) <= d < date.fromisoformat(seg['to'])]
            if not covered:
                continue
            fee = plans[seg['plan_id']]['prices'][currency]['subscription_fee_minor']
            document_lines.append({'kind': 'subscription', 'plan_id': seg['plan_id'], 'days': len(covered),
                                   'amount_minor': half_up(fee * len(covered), period['days_in_period'])})
            if period_dates[-1] in covered:
                end_plan = seg['plan_id']
        check(end_plan is not None, f'{account["account_id"]}: no plan covers the final local day')
        for metric in metrics:
            tiers = plans[end_plan]['prices'][currency]['metrics'][metric]
            for lower, upper, take, price in bracket_split(usage[account['account_id']][metric], tiers):
                document_lines.append({'kind': 'usage', 'metric': metric, 'tier_from': lower, 'tier_to': upper, 'units': take,
                                       'amount_minor': half_up(take * price, 10000)})
        subtotal = sum(l['amount_minor'] for l in document_lines)
        applied = min(subtotal, account['credit_minor'])
        invoices.append({'account_id': account['account_id'], 'currency': currency, 'timezone': account['timezone'],
                         'billable_units': {m: usage[account['account_id']][m] for m in metrics}, 'lines': document_lines,
                         'subtotal_minor': subtotal, 'credit_applied_minor': applied,
                         'credit_remaining_minor': account['credit_minor'] - applied, 'total_minor': subtotal - applied,
                         'quarantined_count': quarantined_by_account[account['account_id']]})
    return invoices, quarantine, Counter(statuses.values())


def run_cli(out: Path, cwd: Path, env: dict, prefix=(), flags=()) -> dict:
    command = [*prefix, sys.executable, *flags, str(ROOT / 'cli.py'), 'run', '--input-dir', str(ROOT / 'data'), '--out', str(out)]
    env = {**{key: os.environ[key] for key in ("SystemRoot", "WINDIR") if key in os.environ}, **env}
    run = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True)
    check(run.returncode == 0, f'CLI run failed ({" ".join(command)}): exit {run.returncode}\n{run.stderr[-2000:]}')
    files = {name: (out / name).read_bytes() for name in OUTPUT_FILES}
    return {'command': ' '.join(command), 'cwd': str(cwd), 'tz': env.get('TZ', 'inherited'), 'exit_code': run.returncode,
            'sha256': {name: sha256(data) for name, data in files.items()}, 'files': files}


def clean_runs() -> tuple[list, dict]:
    runs = []
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        runs.append(run_cli(base / 'a', ROOT, dict(os.environ)))
        runs.append(run_cli(base / 'b', base, {'PATH': os.environ.get('PATH', ''), 'TZ': 'Pacific/Honolulu', 'PYTHONHASHSEED': '3'}))
        runs.append(run_cli(base / 'c', base / 'a', {'PATH': os.environ.get('PATH', ''), 'TZ': 'Asia/Tokyo', 'LANG': 'C'}, flags=('-I',)))
        if shutil.which('unshare') and subprocess.run(['unshare', '-rn', 'true'], capture_output=True).returncode == 0:
            runs.append(run_cli(base / 'd', base, {'PATH': os.environ.get('PATH', ''), 'TZ': 'America/New_York'}, prefix=('unshare', '-rn')))
            runs[-1]['network'] = 'none (unshare -rn)'
        files = runs[0].pop('files')
        for run in runs[1:]:
            run.pop('files')
            check(run['sha256'] == runs[0]['sha256'], f'run outputs differ: {run["command"]} vs {runs[0]["command"]}')
    return runs, files


def main() -> dict:
    raw, archive_sha = load_inputs()
    accounts = json.loads(raw['accounts.json'].decode('utf-8'))
    period = json.loads(raw['period.json'].decode('utf-8'))
    plans = json.loads(raw['plans.json'].decode('utf-8'))
    expected_invoices, expected_quarantine, status_counts = expected_documents(raw, accounts, period, plans)

    runs, files = clean_runs()
    invoices = json.loads(files['invoices.json'].decode('utf-8'))
    quarantine = json.loads(files['quarantine.json'].decode('utf-8'))
    audit = json.loads(files['audit.json'].decode('utf-8'))
    manifest = json.loads(files['manifest.json'].decode('utf-8'))

    check(len(invoices) == len(expected_invoices) == len(accounts), 'one invoice per account expected')
    mismatches = [(a['account_id'], a, b) for a, b in zip(invoices, expected_invoices) if a != b]
    check(not mismatches, f'{len(mismatches)} invoice(s) differ from the independent recomputation; first: {mismatches[:1]}')
    check(len(quarantine) == len(expected_quarantine) == status_counts['quarantined'], 'quarantine.json size differs from the independent status map')
    for actual, expected in zip(quarantine, expected_quarantine):
        check(actual['event_id'] == expected['event_id'] and isinstance(actual['reason'], str) and actual['reason'], f'quarantine entry differs: {actual} vs {expected}')
        check(expected['reason'] is None or actual['reason'] == expected['reason'], f'quarantine reason differs: {actual} vs {expected}')
    for status, count in status_counts.items():
        check(manifest['counts'][status] == count, f'manifest count {status} differs from the independent map')
    check(manifest['counts']['raw'] == sum(status_counts.values()), 'manifest raw count does not reconcile')
    check(manifest['inputs'] == {name: {'sha256': SOURCE_PROVENANCE['input_sha256'][name], 'bytes': len(raw[name]), 'records': manifest['inputs'][name]['records']}
                                 for name in manifest['inputs']} and set(manifest['inputs']) == set(raw), 'manifest input hashes differ from the pinned inputs')
    for name in ('invoices.json', 'quarantine.json', 'audit.json'):
        check(manifest['outputs'][name]['sha256'] == sha256(files[name]), f'manifest hash for {name} differs from the file')
    text = files['manifest.json'].decode('utf-8').lower()
    for word in FORBIDDEN_MANIFEST_WORDS:
        check(word.lower() not in text, f'manifest contains forbidden content {word!r}')
    reconcile(invoices, quarantine, audit, manifest)   # the product's own invariant check, after the independent one

    totals = {}
    for inv in invoices:
        bucket = totals.setdefault(inv['currency'], {'invoices': 0, 'subtotal_minor': 0, 'credit_applied_minor': 0, 'credit_remaining_minor': 0, 'total_minor': 0})
        bucket['invoices'] += 1
        for key in ('subtotal_minor', 'credit_applied_minor', 'credit_remaining_minor', 'total_minor'):
            bucket[key] += inv[key]
    per_account = {inv['account_id']: {'currency': inv['currency'], 'subtotal_minor': inv['subtotal_minor'], 'credit_applied_minor': inv['credit_applied_minor'],
                                       'total_minor': inv['total_minor'], 'lines': len(inv['lines']), 'quarantined_count': inv['quarantined_count'],
                                       'billable_units': inv['billable_units']} for inv in invoices}

    suite_runs, tests, transcript = verified_suite(ROOT, ZONES)
    (ROOT / OWN_OUTPUTS[0]).write_text(transcript)
    receipt = {
        'gate': 'end_to_end_invoices_outputs_and_determinism', 'revision': 'uncommitted',
        'timestamp': datetime.now(timezone.utc).isoformat(), 'python': interpreter(),
        'source_archive_sha256': archive_sha, 'source_provenance': SOURCE_PROVENANCE, 'input_hashes': {k: sha256(v) for k, v in raw.items()},
        'suite_runs': suite_runs, 'tests': tests, 'suite_log': OWN_OUTPUTS[0],
        'historical_red': historical_red(ROOT, 'ledger-modules-red.log'),
        'clean_runs': runs, 'output_hashes': runs[0]['sha256'], 'counts': manifest['counts'], 'quarantine_reasons': manifest['quarantine_reasons'],
        'totals_by_currency': totals, 'per_account': per_account,
        'independent_check': {
            'method': 'statuses from verify_timing.independent_statuses (raw bytes, no product modules); accepted payloads re-read with the plain json '
                      'module; segment days by walking every local period date against raw plan_segments; period-end plan = raw segment containing the '
                      'final local date; tiers split by a private bracket walk; every amount Fraction half-up; credit min(subtotal, credit_minor); '
                      'quarantined_count from quarantined statuses with a known account_id; whole contract documents compared for equality; '
                      'quarantine event_ids and primary reasons re-derived with the documented precedence; manifest counts and hashes compared; '
                      'then audit.reconcile on the published files',
            'invoices_checked': len(invoices), 'quarantine_entries_checked': len(quarantine), 'clean_runs_compared': len(runs)},
        'artifact_hashes': artifact_hashes(ROOT, OWN_OUTPUTS), 'artifact_hash_scope': hash_scope(ROOT, OWN_OUTPUTS),
        'limitations': ['The reference invoices of the grader are not available; equality is against an independent recomputation of the same rules',
                        'Reasons for unreadable lines (no payload) are checked for presence only; the supplied data has none',
                        'The no-network run depends on unshare being permitted on the host; its presence is recorded per run'],
    }
    write_receipt(ROOT, 'invoices.receipt.json', receipt)
    return {'invoices': len(invoices), 'quarantine_entries': len(quarantine), 'counts': manifest['counts'], 'totals_by_currency': totals,
            'clean_runs': [(r['tz'], r.get('network', 'inherited'), r['exit_code']) for r in runs], 'tests': tests,
            'suite_runs': {tz: run['exit_code'] for tz, run in suite_runs.items()}}


if __name__ == '__main__':
    try:
        print(json.dumps(main(), indent=2))
    except (VerificationError, InputError) as exc:
        print(f'VERIFICATION FAILED: {exc}', file=sys.stderr)
        sys.exit(2)
