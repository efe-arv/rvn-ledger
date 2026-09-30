"""Reproduce aggregation and independently reconcile source units.

Uses the product glue (`selection.classify_events`) rather than a private copy
of the pipeline; only `AcceptedEvent` values reach `aggregate_usage`.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from rvn_ledger.aggregation import aggregate_usage
from rvn_ledger.inputs import InputError, read_events, read_json
from rvn_ledger.selection import classify_events
from verify_support import (SOURCE_PROVENANCE, VerificationError, artifact_hashes, check, hash_scope, historical_red,
                            interpreter, load_inputs, sha256, verified_suite, write_receipt)

ROOT = Path(__file__).resolve().parent
OWN_OUTPUTS = ('aggregation-verify-suite.log',)


def main() -> dict:
    raw, archive_sha = load_inputs()
    accounts = read_json(raw['accounts.json'], 'accounts')[0]
    period = read_json(raw['period.json'], 'period')[0]
    rows = read_events(raw['events.jsonl'])
    result = classify_events(rows, accounts, period)
    check(result.counts['raw'] == len(rows) == sum(v for k, v in result.counts.items() if k != 'raw'), 'terminal counts do not reconcile')
    account_ids = [a['account_id'] for a in accounts]
    metrics = period['metrics']
    usage = aggregate_usage(result.accepted, account_ids, metrics)

    # Independent reconciliation against direct filtering of the accepted rows.
    accepted_rows = [event.row for event in result.accepted]
    check(list(usage) == sorted(account_ids), 'account keys are not in sorted order')
    checked_groups = 0
    for account, groups in usage.items():
        check(list(groups) == sorted(metrics), f'{account}: metric keys are not in sorted order')
        for metric, group in groups.items():
            source = [r for r in accepted_rows if r.value['account_id'] == account and r.value['metric'] == metric]
            check(group['units'] == sum(r.value['units'] for r in source), f'{account}/{metric}: units do not reconcile')
            expected_refs = sorted((r.value['event_id'], r.line_number, r.sha256, r.value['units']) for r in source)
            actual_refs = sorted((s['event_id'], s['line_number'], s['sha256'], s['units']) for s in group['sources'])
            check(expected_refs == actual_refs, f'{account}/{metric}: source references do not reconcile')
            checked_groups += 1
    total_sources = sum(len(g['sources']) for groups in usage.values() for g in groups.values())
    check(total_sources == len(accepted_rows) == result.counts.get('accepted', 0), 'every accepted row must appear exactly once as a source')
    check(usage == aggregate_usage(list(reversed(result.accepted)), account_ids, metrics), 'input order changed the aggregation')
    check(json.dumps(usage, sort_keys=True) == json.dumps(aggregate_usage(result.accepted, account_ids, metrics), sort_keys=True),
          'aggregation is not byte-stable across runs')
    totals = {m: sum(groups[m]['units'] for groups in usage.values()) for m in metrics}
    check(sum(totals.values()) == sum(r.value['units'] for r in accepted_rows), 'metric totals do not conserve accepted units')

    runs, tests, transcript = verified_suite(ROOT)
    (ROOT / 'aggregation-verify-suite.log').write_text(transcript)
    receipt = {
        'gate': 'accepted_usage_aggregation', 'revision': 'uncommitted',
        'timestamp': datetime.now(timezone.utc).isoformat(), 'python': interpreter(),
        'source_archive_sha256': archive_sha, 'source_provenance': SOURCE_PROVENANCE,
        'input_hashes': {k: sha256(v) for k, v in raw.items()},
        'suite_runs': runs, 'tests': tests, 'suite_log': 'aggregation-verify-suite.log',
        'historical_red': historical_red(ROOT, 'aggregation-red.log'),
        'counts': result.counts, 'accounts': len(usage), 'metric_totals': totals,
        'independent_check': {
            'method': 'every account/metric total and source tuple compared against direct filtering of accepted rows; '
                      'sorted key order, reversed-input equality, serialized byte stability and unit conservation checked',
            'groups_checked': checked_groups, 'sources_checked': total_sources},
        'artifact_hashes': artifact_hashes(ROOT, OWN_OUTPUTS), 'artifact_hash_scope': hash_scope(ROOT, OWN_OUTPUTS),
        'aggregation': usage,
        'limitations': ['No invoice pricing or invoice outputs in this gate',
                        'Aggregation takes only AcceptedEvent values; time filtering happens in selection.classify_events'],
    }
    write_receipt(ROOT, 'aggregation.receipt.json', receipt)
    return {'counts': result.counts, 'tests': tests, 'accounts': len(usage), 'metric_totals': totals,
            'suite_runs': {tz: run['exit_code'] for tz, run in runs.items()}}


if __name__ == '__main__':
    try:
        print(json.dumps(main(), indent=2))
    except (VerificationError, InputError) as exc:
        print(f'VERIFICATION FAILED: {exc}', file=sys.stderr)
        sys.exit(2)
