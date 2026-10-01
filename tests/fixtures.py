"""Shared test fixtures: one hand-verified input set, its expected outputs, and CLI helpers.

The fixture below covers every billing hazard in a few lines (see the comment on
ACCOUNTS). Expected values were derived by hand, not by running the code.
"""
import hashlib
import json
import os
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

from rvn_ledger.inputs import read_events

# Tariffs mirror the shape of plans.json; numbers chosen so every boundary and rounding case is hand-checkable.
TIER_PLANS = {
    'starter': {'plan_id': 'starter', 'prices': {
        'USD': {'subscription_fee_minor': 2900, 'metrics': {
            'api_calls': [{'from_units': 0, 'to_units': 10000, 'unit_price_micros': 2500},
                          {'from_units': 10000, 'to_units': None, 'unit_price_micros': 1800}],
            'storage_gb_hours': [{'from_units': 0, 'to_units': None, 'unit_price_micros': 1250}]}},
        'TRY': {'subscription_fee_minor': 99900, 'metrics': {
            'api_calls': [{'from_units': 0, 'to_units': 10000, 'unit_price_micros': 86000},
                          {'from_units': 10000, 'to_units': None, 'unit_price_micros': 61000}],
            'storage_gb_hours': [{'from_units': 0, 'to_units': None, 'unit_price_micros': 42000}]}}}},
    'growth': {'plan_id': 'growth', 'prices': {
        'USD': {'subscription_fee_minor': 9900, 'metrics': {
            'api_calls': [{'from_units': 0, 'to_units': 50000, 'unit_price_micros': 1900},
                          {'from_units': 50000, 'to_units': 250000, 'unit_price_micros': 1400},
                          {'from_units': 250000, 'to_units': None, 'unit_price_micros': 900}],
            'storage_gb_hours': [{'from_units': 0, 'to_units': 5000, 'unit_price_micros': 1100},
                                 {'from_units': 5000, 'to_units': None, 'unit_price_micros': 800}]}}}},
}


def reference_line(units, price_micros):
    # Independent reference: exact rational arithmetic, half rounds up.
    return int(Fraction(units * price_micros, 10000) + Fraction(1, 2))


PLANS = json.loads(json.dumps(TIER_PLANS))
PLANS['scale'] = {'plan_id': 'scale', 'prices': {'EUR': {'subscription_fee_minor': 27900, 'metrics': {
    'api_calls': [{'from_units': 0, 'to_units': 250000, 'unit_price_micros': 1100}, {'from_units': 250000, 'to_units': None, 'unit_price_micros': 650}],
    'storage_gb_hours': [{'from_units': 0, 'to_units': None, 'unit_price_micros': 600}]}}}}
PLANS['starter']['prices']['EUR'] = {'subscription_fee_minor': 2700, 'metrics': {
    'api_calls': [{'from_units': 0, 'to_units': 10000, 'unit_price_micros': 2300}, {'from_units': 10000, 'to_units': None, 'unit_price_micros': 1650}],
    'storage_gb_hours': [{'from_units': 0, 'to_units': None, 'unit_price_micros': 1150}]}}
PERIOD = {'period_start_local': '2026-09-01', 'period_end_local_exclusive': '2026-10-01', 'days_in_period': 30,
          'late_cutoff_hours_after_period_end': 48, 'metrics': ['api_calls', 'storage_gb_hours']}


def account(account_id, zone, currency, credit, segments):
    return {'account_id': account_id, 'name': account_id, 'timezone': zone, 'currency': currency, 'credit_minor': credit,
            'plan_segments': [{'plan_id': p, 'from': f, 'to': t} for p, f, t in segments]}


def event(event_id, seq, account_id, metric='api_calls', units=1, ts='2026-09-10T00:00:00Z', ingested_at='2026-09-10T00:01:00Z', **extra):
    return {'event_id': event_id, 'ingest_seq': seq, 'account_id': account_id, 'metric': metric, 'units': units,
            'ts': ts, 'ingested_at': ingested_at, **extra}


def raw_lines(values):
    return ('\n'.join(v if isinstance(v, str) else json.dumps(v) for v in values) + '\n').encode()


# Hand-verified hazard fixture (see tests/test_pipeline.py for the full expected outputs):
#   acct_a Europe/Istanbul TRY starter, credit 500     - usage crossing a tier, duplicate with different payload,
#                                                        out-of-period, late, exact late boundary, two quarantines
#   acct_b America/New_York USD starter->growth 09-17  - plan change mid period, usage priced on growth
#   acct_c UTC EUR scale, credit 100000                - no usage, credit exceeds subtotal
#   acct_d Asia/Tokyo USD growth, credit 1000          - storage across tiers, api line rounding to 1 minor unit
ACCOUNTS = [
    account('acct_a', 'Europe/Istanbul', 'TRY', 500, [('starter', '2026-09-01', '2026-10-01')]),
    account('acct_b', 'America/New_York', 'USD', 0, [('starter', '2026-09-01', '2026-09-17'), ('growth', '2026-09-17', '2026-10-01')]),
    account('acct_c', 'UTC', 'EUR', 100000, [('scale', '2026-09-01', '2026-10-01')]),
    account('acct_d', 'Asia/Tokyo', 'USD', 1000, [('growth', '2026-09-01', '2026-10-01')]),
]
EVENTS = [
    event('a1', 1, 'acct_a', units=4000),                                                                      # 1 accepted
    event('a2', 5, 'acct_a', units=8000, ts='2026-09-20T12:00:00Z', ingested_at='2026-09-20T12:00:00Z'),        # 2 accepted (seq 5 < line 3's 9)
    event('a2', 9, 'acct_a', units=999999),                                                                     # 3 duplicate, different payload
    event('a3', 3, 'acct_a', metric='storage_gb_hours', units=100),                                             # 4 accepted
    event('a4', 4, 'acct_a', units=5, ts='2026-08-31T20:59:59Z'),                                               # 5 out of period (Istanbul 23:59:59 Aug 31)
    event('a5', 6, 'acct_a', units=5, ts='2026-09-30T20:59:59Z', ingested_at='2026-10-02T21:00:01Z'),          # 6 late by one second
    event('a6', 7, 'acct_a', units=5, ts='2026-09-30T20:59:59Z', ingested_at='2026-10-02T21:00:00Z'),          # 7 accepted: exactly 48h
    event('a7', 8, 'acct_a', units=0),                                                                          # 8 quarantined nonpositive_units
    event('a8', 10, 'acct_a', units='12'),                                                                      # 9 quarantined invalid_units
    event('b1', 12, 'acct_b', units=50000),                                                                     # 10 accepted
    event('b2', 11, 'acct_b', units=10000, ts='2026-09-01T04:00:00Z'),                                          # 11 accepted: NY period start exactly
    event('b3', 13, 'acct_b', metric='gpu', units=7),                                                           # 12 quarantined unknown_metric
    event('z1', 14, 'acct_zzz', units=7),                                                                       # 13 quarantined unknown_account
    event('d1', 16, 'acct_d', metric='storage_gb_hours', units=7500),                                           # 14 accepted
    event('d2', 15, 'acct_d', units=3),                                                                         # 15 accepted (out of order, fine)
    event('d3', 17, 'acct_d', units=-5),                                                                        # 16 quarantined nonpositive_units
    event('c1', 18, 'acct_c', units=4, ts='2026-09-10 00:00:00'),                                               # 17 quarantined invalid_ts
    '{broken',                                                                                                  # 18 quarantined invalid_json, no identity
]
RAW_EVENTS = raw_lines(EVENTS)

EXPECTED_INVOICES = [
    {'account_id': 'acct_a', 'currency': 'TRY', 'timezone': 'Europe/Istanbul',
     'billable_units': {'api_calls': 12005, 'storage_gb_hours': 100},
     'lines': [{'kind': 'subscription', 'plan_id': 'starter', 'days': 30, 'amount_minor': 99900},
               {'kind': 'usage', 'metric': 'api_calls', 'tier_from': 0, 'tier_to': 10000, 'units': 10000, 'amount_minor': 86000},
               {'kind': 'usage', 'metric': 'api_calls', 'tier_from': 10000, 'tier_to': None, 'units': 2005, 'amount_minor': 12231},
               {'kind': 'usage', 'metric': 'storage_gb_hours', 'tier_from': 0, 'tier_to': None, 'units': 100, 'amount_minor': 420}],
     'subtotal_minor': 198551, 'credit_applied_minor': 500, 'credit_remaining_minor': 0, 'total_minor': 198051, 'quarantined_count': 2},
    {'account_id': 'acct_b', 'currency': 'USD', 'timezone': 'America/New_York',
     'billable_units': {'api_calls': 60000, 'storage_gb_hours': 0},
     'lines': [{'kind': 'subscription', 'plan_id': 'starter', 'days': 16, 'amount_minor': 1547},
               {'kind': 'subscription', 'plan_id': 'growth', 'days': 14, 'amount_minor': 4620},
               {'kind': 'usage', 'metric': 'api_calls', 'tier_from': 0, 'tier_to': 50000, 'units': 50000, 'amount_minor': 9500},
               {'kind': 'usage', 'metric': 'api_calls', 'tier_from': 50000, 'tier_to': 250000, 'units': 10000, 'amount_minor': 1400}],
     'subtotal_minor': 17067, 'credit_applied_minor': 0, 'credit_remaining_minor': 0, 'total_minor': 17067, 'quarantined_count': 1},
    {'account_id': 'acct_c', 'currency': 'EUR', 'timezone': 'UTC',
     'billable_units': {'api_calls': 0, 'storage_gb_hours': 0},
     'lines': [{'kind': 'subscription', 'plan_id': 'scale', 'days': 30, 'amount_minor': 27900}],
     'subtotal_minor': 27900, 'credit_applied_minor': 27900, 'credit_remaining_minor': 72100, 'total_minor': 0, 'quarantined_count': 1},
    {'account_id': 'acct_d', 'currency': 'USD', 'timezone': 'Asia/Tokyo',
     'billable_units': {'api_calls': 3, 'storage_gb_hours': 7500},
     'lines': [{'kind': 'subscription', 'plan_id': 'growth', 'days': 30, 'amount_minor': 9900},
               {'kind': 'usage', 'metric': 'api_calls', 'tier_from': 0, 'tier_to': 50000, 'units': 3, 'amount_minor': 1},
               {'kind': 'usage', 'metric': 'storage_gb_hours', 'tier_from': 0, 'tier_to': 5000, 'units': 5000, 'amount_minor': 550},
               {'kind': 'usage', 'metric': 'storage_gb_hours', 'tier_from': 5000, 'tier_to': None, 'units': 2500, 'amount_minor': 200}],
     'subtotal_minor': 10651, 'credit_applied_minor': 1000, 'credit_remaining_minor': 0, 'total_minor': 9651, 'quarantined_count': 1},
]
EXPECTED_QUARANTINE = [
    {'event_id': 'a7', 'reason': 'nonpositive_units'}, {'event_id': 'a8', 'reason': 'invalid_units'},
    {'event_id': 'b3', 'reason': 'unknown_metric'}, {'event_id': 'z1', 'reason': 'unknown_account'},
    {'event_id': 'd3', 'reason': 'nonpositive_units'}, {'event_id': 'c1', 'reason': 'invalid_ts'},
    {'event_id': None, 'reason': 'invalid_json'},
]


def build(accounts=ACCOUNTS, raw=RAW_EVENTS, period=PERIOD, plans=PLANS):
    from rvn_ledger.invoice import build_invoices
    from rvn_ledger.selection import classify_events
    rows = read_events(raw)
    classification = classify_events(rows, accounts, period)
    return build_invoices(accounts, period, plans, rows, classification)


ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / 'data'
CLI_ARGS = [sys.executable, '-m', 'rvn_ledger']   # the installed package, as a user runs it


def write_fixture(directory: Path, accounts=ACCOUNTS, raw=RAW_EVENTS, period=PERIOD, plans=PLANS):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / 'events.jsonl').write_bytes(raw)
    (directory / 'accounts.json').write_text(json.dumps(accounts))
    (directory / 'plans.json').write_text(json.dumps(plans))
    (directory / 'period.json').write_text(json.dumps(period))
    return directory


def run_cli(*args, cwd=None, env=None):
    return subprocess.run([*CLI_ARGS, *map(str, args)], cwd=cwd, env=env, capture_output=True, text=True)


def snapshot(directory: Path) -> dict:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(directory.iterdir())}


def run_fixture():
    from rvn_ledger.pipeline import run_ledger
    raw = {'events.jsonl': RAW_EVENTS, 'accounts.json': json.dumps(ACCOUNTS).encode(), 'plans.json': json.dumps(PLANS).encode(),
           'period.json': json.dumps(PERIOD).encode()}
    return run_ledger(raw)


INPUT_NAMES = ('events.jsonl', 'accounts.json', 'plans.json', 'period.json')
SCRATCH = os.environ.get('TMPDIR')


def publish_run(run, directory):
    from rvn_ledger.outputs import publish, serialize
    publish(directory, {'invoices.json': serialize(run.invoices), 'quarantine.json': serialize(run.quarantine),
                        'audit.json': serialize(run.audit)}, run.manifest)


def rewrite_manifest(out, mutate):
    """Edit a published manifest in place; manifest.json carries no self-hash, so only reconciliation can notice."""
    path = Path(out) / 'manifest.json'
    manifest = json.loads(path.read_bytes())
    mutate(manifest)
    path.write_bytes(json.dumps(manifest, indent=2).encode() + b'\n')
