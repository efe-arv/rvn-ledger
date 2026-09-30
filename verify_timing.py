"""Reproduce the timing gate against the immutable supplied inputs.

Uses the product glue (`selection.classify_events`) rather than a private copy
of the pipeline, then re-derives EVERY raw line's terminal status on its own
(`independent_statuses`): the line is parsed into ordered pair lists with
integer tokens instead of the product's dict hooks, identity and first copy by
`ingest_seq` are chosen again, field validity is re-checked, and the period is
resolved from the account's zone and the local dates with a late cutoff
computed from the local end date. A valid winner that the pipeline gives the
wrong status is therefore a verification failure, not something the check
inherits from the pipeline under test.
"""
import json
import math
import re
from fractions import Fraction
import sys
from collections import Counter
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from rvn_ledger.inputs import InputError, read_events, read_json
from rvn_ledger.selection import classify_events
from verify_support import (SOURCE_PROVENANCE, VerificationError, artifact_hashes, check, hash_scope, historical_red,
                            interpreter, load_inputs, sha256, verified_suite, write_receipt)

ROOT = Path(__file__).resolve().parent
ZONES = ('UTC', 'Pacific/Honolulu')
OWN_OUTPUTS = ('timing-verify-suite.log',)


class _Object(list):
    """A JSON object kept as its ordered (key, value) pairs; JSON arrays stay plain lists."""


def _int_token(text: str) -> tuple:
    return ('int', text)  # integers stay tokens: never converted, never overflow, never confused with strings


def _clock_parts(text):
    """Independent strptime whole-second parser plus a lossless rational fraction."""
    if not isinstance(text, str):
        return None, None
    match = re.fullmatch(r'([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2})(?:[.,]([0-9]+))?(Z|[+-][0-9]{2}:[0-9]{2})', text)
    if not match:
        return None, None
    whole, digits, offset = match.groups()
    try:
        parsed = datetime.strptime(whole + offset, '%Y-%m-%dT%H:%M:%S%z').astimezone(timezone.utc)
        delta = parsed - datetime(1970, 1, 1, tzinfo=timezone.utc)
        fractional = Fraction(int(digits), 10 ** len(digits)) if digits else Fraction(0)
        exact = Fraction(delta.days * 86400 + delta.seconds) + fractional
        return parsed.replace(microsecond=int(fractional * 1000000)), exact
    except (ValueError, OverflowError):
        return None, None


def instant(text) -> datetime | None:
    """UTC datetime compatibility view; exact fractions are retained by _clock_parts."""
    return _clock_parts(text)[0]


def in_local_period(ts: datetime, zone: ZoneInfo, start_local: str, end_local: str) -> bool:
    """Does the UTC instant fall on a local calendar date of [start, end)?"""
    try:
        local_day = ts.astimezone(zone).date().isoformat()
    except OverflowError:
        return False  # beyond the representable range in that zone: certainly not a 2026 date (review F1)
    return start_local <= local_day < end_local


def compare_statuses(pipeline: dict[int, str], oracle: dict[int, str]) -> list:
    """The verification decision itself (review F2): identical maps or a VerificationError naming the lines."""
    check(set(pipeline) == set(oracle), 'independent check and pipeline disagree on the set of raw lines')
    mismatches = sorted((line, pipeline[line], status) for line, status in oracle.items() if pipeline[line] != status)
    check(not mismatches, f'{len(mismatches)} line(s) differ from the independent check (line, pipeline, independent): {mismatches[:5]}')
    return mismatches


def _strict(tree, limit: int) -> bool:
    """Would a strict reader accept this tree: unique keys everywhere, finite numbers, bounded integers."""
    stack = [tree]
    while stack:
        node = stack.pop()
        if isinstance(node, _Object):
            keys = [key for key, _ in node]
            if len(set(keys)) != len(keys):
                return False
            stack.extend(keys)
            stack.extend(value for _, value in node)
        elif isinstance(node, str) and any(0xD800 <= ord(c) <= 0xDFFF for c in node):
            return False
        elif isinstance(node, list):
            stack.extend(node)
        elif isinstance(node, tuple):
            if limit and len(node[1].lstrip('-')) > limit:
                return False
        elif isinstance(node, float) and not math.isfinite(node):
            return False
    return True


def _identity(tree, limit: int):
    """(event_id, ingest_seq) when the top-level object states each exactly once and readably."""
    if not isinstance(tree, _Object):
        return None
    keys = [key for key, _ in tree]
    if keys.count('event_id') != 1 or keys.count('ingest_seq') != 1:
        return None
    fields = dict(tree)
    event_id, sequence = fields['event_id'], fields['ingest_seq']
    if not isinstance(event_id, str) or not event_id or not isinstance(sequence, tuple):
        return None
    if limit and len(sequence[1].lstrip('-')) > limit:
        return None
    return event_id, int(sequence[1])


def independent_statuses(raw_events: bytes, accounts: list, period: dict) -> dict[int, str]:
    """Every raw line -> terminal status, derived without inputs/selection/validation/timing."""
    limit = sys.get_int_max_str_digits()
    zones = {account['account_id']: ZoneInfo(account['timezone']) for account in accounts}
    metrics = set(period['metrics'])
    start_local, end_local = period['period_start_local'], period['period_end_local_exclusive']
    late = timedelta(hours=period['late_cutoff_hours_after_period_end'])
    cutoffs = {account: datetime.combine(date.fromisoformat(end_local), time.min, zone).astimezone(timezone.utc) + late
               for account, zone in zones.items()}
    lines = raw_events.split(b'\n')
    if lines and lines[-1] == b'':
        lines.pop()
    identities, payloads = {}, {}
    for number, line in enumerate(lines, 1):
        try:
            tree = json.loads(line.decode('utf-8'), object_pairs_hook=_Object, parse_int=_int_token)
        except (UnicodeDecodeError, ValueError, RecursionError):
            continue
        identity = _identity(tree, limit)
        if identity is None:
            continue
        identities[number] = identity
        if _strict(tree, limit):
            payloads[number] = dict(tree)
    first = {}
    for number, (event_id, sequence) in identities.items():
        if event_id not in first or (sequence, number) < first[event_id]:
            first[event_id] = (sequence, number)
    winners = {number for _, number in first.values()}
    statuses = {}
    for number in range(1, len(lines) + 1):
        if number not in identities:
            statuses[number] = 'quarantined'
        elif number not in winners:
            statuses[number] = 'duplicate_ignored'
        elif number not in payloads:
            statuses[number] = 'quarantined'
        else:
            fields = payloads[number]
            account, metric, units = fields.get('account_id'), fields.get('metric'), fields.get('units')
            ts, ingested_at = instant(fields.get('ts')), instant(fields.get('ingested_at'))
            if (not isinstance(account, str) or account not in zones or not isinstance(metric, str) or metric not in metrics
                    or not isinstance(units, tuple) or int(units[1]) <= 0 or ts is None or ingested_at is None):
                statuses[number] = 'quarantined'
            elif not in_local_period(ts, zones[account], start_local, end_local):
                statuses[number] = 'excluded_out_of_period'
            elif _clock_parts(fields['ingested_at'])[1] > Fraction((cutoffs[account] - datetime(1970, 1, 1, tzinfo=timezone.utc)).days * 86400
                                                                  + (cutoffs[account] - datetime(1970, 1, 1, tzinfo=timezone.utc)).seconds):
                statuses[number] = 'excluded_late'
            else:
                statuses[number] = 'accepted'
    return statuses


def main() -> dict:
    raw, archive_sha = load_inputs()
    accounts = read_json(raw['accounts.json'], 'accounts')[0]
    period = read_json(raw['period.json'], 'period')[0]
    rows = read_events(raw['events.jsonl'])
    result = classify_events(rows, accounts, period)
    check(result.statuses == classify_events(rows, accounts, period).statuses, 'classification is not deterministic')
    check(result.counts['raw'] == len(rows) == len(result.statuses), 'status count does not match raw line count')
    check(result.counts['raw'] == sum(v for k, v in result.counts.items() if k != 'raw'), 'terminal counts do not reconcile')
    check(sorted(e.row.line_number for e in result.accepted) == sorted(l for l, s in result.statuses.items() if s == 'accepted'),
          'accepted list disagrees with statuses')

    # Independent re-derivation of every line's status; the set of valid winners comes from
    # the raw bytes, not from the pipeline under test, so a misclassified winner is caught.
    expected = independent_statuses(raw['events.jsonl'], accounts, period)
    check(set(expected) == set(range(1, len(rows) + 1)), 'independent check does not cover every raw line')
    mismatches = compare_statuses(result.statuses, expected)
    checked = Counter(expected.values())

    runs, tests, transcript = verified_suite(ROOT, ZONES)
    (ROOT / OWN_OUTPUTS[0]).write_text(transcript)
    decisions = [{'line_number': row.line_number, 'status': result.statuses[row.line_number],
                  'event_id': (row.value or {}).get('event_id') if row.value else
                  (row.salvaged_identity.event_id if row.salvaged_identity else None)} for row in rows]
    receipt = {
        'gate': 'account_local_period_and_late_cutoff', 'revision': 'uncommitted',
        'timestamp': datetime.now(timezone.utc).isoformat(), 'python': interpreter(),
        'source_archive_sha256': archive_sha, 'source_provenance': SOURCE_PROVENANCE,
        'input_hashes': {k: sha256(v) for k, v in raw.items()},
        'suite_runs': runs, 'tests': tests, 'suite_log': OWN_OUTPUTS[0],
        'historical_red': historical_red(ROOT, 'timing-red.log'),
        'counts': result.counts, 'independent_check': {
            'method': 'every raw line re-derived from the raw bytes without the product modules: pair-list parse with integer '
                      'tokens, strictness (unique keys, finite numbers, bounded integers), top-level identity, first copy by '
                      '(ingest_seq, line), field validity, account-local calendar date of ts against the period dates, late '
                      'cutoff recomputed as local end-date midnight in the account zone converted to UTC plus the elapsed hours; '
                      'the full status map must equal the pipeline map',
            'lines_checked': sum(checked.values()), 'checked': dict(sorted(checked.items())), 'mismatches': mismatches},
        'artifact_hashes': artifact_hashes(ROOT, OWN_OUTPUTS), 'artifact_hash_scope': hash_scope(ROOT, OWN_OUTPUTS),
        'limitations': ['No invoices or usage aggregation produced by this gate',
                        'DST elapsed-time test is synthetic; supplied billing month is September 2026',
                        'Independent check parses whole seconds with strptime and preserves rational fractions; zones use zoneinfo, the same '
                        'standard library the code uses; the JSON grammar itself is also the standard library decoder'],
        'decisions': decisions,
    }
    write_receipt(ROOT, 'timing.receipt.json', receipt)
    return {'counts': result.counts, 'tests': tests, 'suite_runs': {tz: run['exit_code'] for tz, run in runs.items()},
            'independent_check': dict(sorted(checked.items()))}


if __name__ == '__main__':
    try:
        print(json.dumps(main(), indent=2))
    except (VerificationError, InputError) as exc:
        print(f'VERIFICATION FAILED: {exc}', file=sys.stderr)
        sys.exit(2)
