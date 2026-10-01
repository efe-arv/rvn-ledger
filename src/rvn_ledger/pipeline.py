"""The billing run: four input byte strings in, four deterministic documents out.

Order (architecture section 6): configuration is validated eagerly, events are
read losslessly, deduplicated, validated and time-filtered, accepted usage is
aggregated, subscriptions and tiers are priced, credit is applied, and the
audit trail and manifest are built. No clock, random id or absolute path enters
any document, so two runs on the same inputs are byte-identical.
"""
import hashlib
import platform
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from .aggregation import aggregate_usage
from .audit import build_audit
from .context import prepare_context
from . import __version__
from .inputs import InputError, read_events, read_json
from .invoice import assemble_invoices
from .selection import classify_prepared

LEDGER_VERSION = __version__
RULES_SOURCE = 'AI Digital Engineering Take-Home: Ledger, billing rules 1-9 and the period.json output contract'
INPUT_NAMES = ('events.jsonl', 'accounts.json', 'plans.json', 'period.json')
CODE_MODULES = ('__init__.py', 'context.py', 'inputs.py', 'money.py', 'selection.py', 'validation.py', 'timing.py', 'aggregation.py', 'subscription.py',
                'tiers.py', 'invoice.py', 'audit.py', 'outputs.py', 'pipeline.py', 'cli.py', 'diagnostics.py', 'excel.py')


@dataclass
class LedgerRun:
    invoices: list       # contract documents, sorted by account_id
    quarantine: list     # {event_id, reason} in source line order
    audit: dict
    manifest: dict


def tzdata_version() -> str:
    """The time zone data zoneinfo actually resolves against: the system TZPATH tree first, else the tzdata package."""
    import zoneinfo
    for root in zoneinfo.TZPATH:
        if (Path(root) / 'UTC').is_file():                       # zoneinfo searches TZPATH before the package
            path = Path(root) / 'tzdata.zi'
            first = path.read_text(errors='replace').splitlines()[:1] if path.is_file() else []
            return f'system {first[0].lstrip("# ").strip() if first else "(version file absent)"}'
    try:
        from importlib.metadata import version
        return f'tzdata package {version("tzdata")}'
    except Exception:  # noqa: BLE001 - no system tree and no package: record that honestly
        return 'unknown'


def code_hashes() -> dict:
    here = Path(__file__).resolve().parent
    return {name: hashlib.sha256((here / name).read_bytes()).hexdigest() for name in CODE_MODULES if (here / name).is_file()}


def run_ledger(raw: dict) -> LedgerRun:
    missing = [name for name in INPUT_NAMES if name not in raw]
    if missing:
        raise InputError(f'missing inputs: {", ".join(missing)}')
    accounts, accounts_sha = read_json(raw['accounts.json'], 'accounts.json')
    plans, plans_sha = read_json(raw['plans.json'], 'plans.json')
    period, period_sha = read_json(raw['period.json'], 'period.json')
    if not isinstance(accounts, list) or not accounts:
        raise InputError('accounts.json must be a nonempty list of accounts')
    if not isinstance(plans, dict) or not plans:
        raise InputError('plans.json must be an object keyed by plan_id')
    if not isinstance(period, dict):
        raise InputError('period.json must be an object')
    context = prepare_context(accounts, period, plans)
    rows = read_events(raw['events.jsonl'])
    classification = classify_prepared(rows, context)
    metrics = list(context.metrics)
    usage = aggregate_usage(classification.accepted, context.accounts, metrics)
    invoices = assemble_invoices(context, rows, classification, usage)
    audit = build_audit(rows, classification, invoices, usage, period)
    quarantine = [{'event_id': d['event_id'], 'reason': d['reasons'][0]} for d in audit['decisions'] if d['status'] == 'quarantined']
    documents = [inv.as_contract() for inv in invoices]

    totals = {}
    for inv in documents:
        bucket = totals.setdefault(inv['currency'], {'subtotal_minor': 0, 'credit_applied_minor': 0, 'total_minor': 0, 'invoices': 0})
        bucket['subtotal_minor'] += inv['subtotal_minor']
        bucket['credit_applied_minor'] += inv['credit_applied_minor']
        bucket['total_minor'] += inv['total_minor']
        bucket['invoices'] += 1
    counts = dict(classification.counts)
    for status in ('accepted', 'duplicate_ignored', 'excluded_out_of_period', 'excluded_late', 'quarantined'):
        counts.setdefault(status, 0)
    counts.update({'invoices': len(documents), 'quarantine_entries': len(quarantine),
                   'subscription_lines': sum(len(inv.subscription.segments) for inv in invoices),
                   'usage_lines': sum(len(inv.usage_lines) for inv in invoices)})
    manifest = {
        'ledger_version': LEDGER_VERSION,
        'rules': {'source': RULES_SOURCE, 'period_start_local': period['period_start_local'],
                  'period_end_local_exclusive': period['period_end_local_exclusive'], 'days_in_period': period['days_in_period'],
                  'late_cutoff_hours_after_period_end': period['late_cutoff_hours_after_period_end'], 'metrics': metrics},
        'inputs': {'events.jsonl': {'sha256': hashlib.sha256(raw['events.jsonl']).hexdigest(), 'bytes': len(raw['events.jsonl']), 'records': len(rows)},
                   'accounts.json': {'sha256': accounts_sha, 'bytes': len(raw['accounts.json']), 'records': len(accounts)},
                   'plans.json': {'sha256': plans_sha, 'bytes': len(raw['plans.json']), 'records': len(plans)},
                   'period.json': {'sha256': period_sha, 'bytes': len(raw['period.json']), 'records': 1}},
        'counts': counts,
        'quarantine_reasons': dict(sorted(Counter(entry['reason'] for entry in quarantine).items())),
        'totals_by_currency': dict(sorted(totals.items())),
        'versions': {'python': platform.python_version(), 'implementation': platform.python_implementation(),
                     'tzdata': tzdata_version(), 'code_sha256': code_hashes()},
        'audit_trail': 'audit.json',
    }
    return LedgerRun(documents, quarantine, audit, manifest)
