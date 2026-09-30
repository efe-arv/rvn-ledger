"""Reproduce subscription segmentation on the immutable supplied inputs.

Independent re-derivation for every account: segment days are counted by
walking each local calendar date of the period and testing membership in the
account's raw `plan_segments` (never subtraction of dates), amounts are
recomputed with exact `Fraction` arithmetic and explicit half-up rounding, the
fee is read straight from plans.json in the account currency, and the
period-end plan is the raw segment containing the final local date.
"""
import json
import sys
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path
from rvn_ledger.inputs import InputError, read_json
from rvn_ledger.subscription import all_subscriptions
from verify_support import (SOURCE_PROVENANCE, VerificationError, artifact_hashes, check, hash_scope, historical_red,
                            interpreter, load_inputs, sha256, verified_suite, write_receipt)

ROOT = Path(__file__).resolve().parent
ZONES = ('UTC', 'Pacific/Honolulu')
OWN_OUTPUTS = ('subscription-verify-suite.log',)


def reference_amount(fee: int, days: int, period_days: int) -> int:
    return int(Fraction(fee * days, period_days) + Fraction(1, 2))


def main() -> dict:
    raw, archive_sha = load_inputs()
    accounts = read_json(raw['accounts.json'], 'accounts')[0]
    period = read_json(raw['period.json'], 'period')[0]
    plans = read_json(raw['plans.json'], 'plans')[0]
    subscriptions = all_subscriptions(accounts, period, plans)
    check(subscriptions == all_subscriptions(list(reversed(accounts)), period, plans), 'account order changed the result')
    check(list(subscriptions) == sorted(a['account_id'] for a in accounts), 'every account must appear once, sorted')

    start = date.fromisoformat(period['period_start_local'])
    end = date.fromisoformat(period['period_end_local_exclusive'])
    period_dates = [start + timedelta(days=i) for i in range((end - start).days)]
    check(len(period_dates) == period['days_in_period'] == 30, 'supplied period is not the documented 30 local days')
    last_day = period_dates[-1]

    checked_segments, multi_segment_accounts, currencies = 0, [], Counter()
    for account in accounts:
        sub = subscriptions[account['account_id']]
        check(sub.currency == account['currency'], f'{sub.account_id}: currency mismatch')
        currencies[sub.currency] += 1
        raw_segments = account['plan_segments']
        expected = []
        for seg in sorted(raw_segments, key=lambda s: s['from']):
            covered = [d for d in period_dates if date.fromisoformat(seg['from']) <= d < date.fromisoformat(seg['to'])]
            if not covered:
                continue
            fee = plans[seg['plan_id']]['prices'][account['currency']]['subscription_fee_minor']
            expected.append((seg['plan_id'], covered[0], covered[-1] + timedelta(days=1), len(covered), fee,
                             reference_amount(fee, len(covered), period['days_in_period'])))
        actual = [(s.plan_id, s.start, s.end, s.days, s.fee_minor, s.amount_minor) for s in sub.segments]
        check(actual == expected, f'{sub.account_id}: segments {actual} differ from independent {expected}')
        covered_days = Counter(d for seg in raw_segments for d in period_dates
                               if date.fromisoformat(seg['from']) <= d < date.fromisoformat(seg['to']))
        check(all(n == 1 for n in covered_days.values()), f'{sub.account_id}: a local day is covered more than once')
        check(sum(s.days for s in sub.segments) == len(covered_days), f'{sub.account_id}: segment days do not reconcile')
        end_plans = [seg['plan_id'] for seg in raw_segments
                     if date.fromisoformat(seg['from']) <= last_day < date.fromisoformat(seg['to'])]
        check(end_plans == [sub.period_end_plan_id], f'{sub.account_id}: period-end plan {sub.period_end_plan_id} vs {end_plans}')
        if sum(s.days for s in sub.segments) == 30 and len(sub.segments) == 1:
            check(sub.segments[0].amount_minor == sub.segments[0].fee_minor, f'{sub.account_id}: full period must charge the full fee')
        if len(sub.segments) > 1:
            multi_segment_accounts.append(sub.account_id)
        checked_segments += len(sub.segments)

    serialized = {account_id: {'currency': sub.currency, 'period_end_plan_id': sub.period_end_plan_id,
                               'segments': [{'plan_id': s.plan_id, 'from': s.start.isoformat(), 'to': s.end.isoformat(),
                                             'days': s.days, 'fee_minor': s.fee_minor, 'amount_minor': s.amount_minor}
                                            for s in sub.segments]}
                  for account_id, sub in subscriptions.items()}
    check(json.dumps(serialized, sort_keys=True) == json.dumps({
        account_id: {'currency': sub.currency, 'period_end_plan_id': sub.period_end_plan_id,
                     'segments': [{'plan_id': s.plan_id, 'from': s.start.isoformat(), 'to': s.end.isoformat(),
                                   'days': s.days, 'fee_minor': s.fee_minor, 'amount_minor': s.amount_minor} for s in sub.segments]}
        for account_id, sub in all_subscriptions(accounts, period, plans).items()}, sort_keys=True),
        'segmentation is not byte-stable across runs')

    runs, tests, transcript = verified_suite(ROOT, ZONES)
    (ROOT / 'subscription-verify-suite.log').write_text(transcript)
    subscription_total = {c: sum(s.amount_minor for sub in subscriptions.values() if sub.currency == c for s in sub.segments)
                          for c in sorted(currencies)}
    receipt = {
        'gate': 'subscription_segmentation_and_period_end_plan', 'revision': 'uncommitted',
        'timestamp': datetime.now(timezone.utc).isoformat(), 'python': interpreter(), 'source_archive_sha256': archive_sha,
        'source_provenance': SOURCE_PROVENANCE, 'input_hashes': {k: sha256(v) for k, v in raw.items()},
        'suite_runs': runs, 'tests': tests, 'suite_log': 'subscription-verify-suite.log',
        'historical_red': historical_red(ROOT, 'subscription-red.log'),
        'accounts': len(subscriptions), 'segments': checked_segments, 'multi_segment_accounts': multi_segment_accounts,
        'accounts_by_currency': dict(sorted(currencies.items())),
        'period_end_plans': dict(sorted(Counter(s.period_end_plan_id for s in subscriptions.values()).items())),
        'subscription_total_minor_by_currency': subscription_total,
        'independent_check': {
            'method': 'per account: days counted by walking every local period date against raw plan_segments; '
                      'amount recomputed with Fraction(fee*days, days_in_period) + 1/2 truncated; fee read directly from '
                      'plans.json in the account currency; period-end plan taken from the raw segment containing the final '
                      'local date; no day covered twice; reversed account order and serialized byte stability checked',
            'segments_checked': checked_segments},
        'artifact_hashes': artifact_hashes(ROOT, OWN_OUTPUTS), 'artifact_hash_scope': hash_scope(ROOT, OWN_OUTPUTS),
        'subscriptions': serialized,
        'limitations': ['No usage tiers, invoice assembly or CLI in this gate',
                        'Supplied data has no partial coverage, gaps or segments outside the period; those paths are unit-tested only',
                        'Uncovered mid-period days charging nothing is a documented assumption, not a source rule'],
    }
    write_receipt(ROOT, 'subscription.receipt.json', receipt)
    return {'accounts': len(subscriptions), 'segments': checked_segments, 'multi_segment_accounts': multi_segment_accounts,
            'subscription_total_minor_by_currency': subscription_total, 'tests': tests,
            'suite_runs': {tz: run['exit_code'] for tz, run in runs.items()}}


if __name__ == '__main__':
    try:
        print(json.dumps(main(), indent=2))
    except (VerificationError, InputError) as exc:
        print(f'VERIFICATION FAILED: {exc}', file=sys.stderr)
        sys.exit(2)
