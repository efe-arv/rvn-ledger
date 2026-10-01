"""Audit trail (every raw line's fate, every invoice line's sources) and its independent reconciliation.

`build_audit` records, per raw line, the terminal status with the line's SHA-256
and identity, and per invoice the plan segments, the tier lines with their unit
price and formula, the accepted source events per metric, the credit arithmetic
and the attributed quarantined lines.

`reconcile` is the invariant check the CLI runs before publishing and that
`cli.py check` re-runs on published files. It recomputes every amount from the
trail with exact rational arithmetic (not the product's money functions), ties
every usage line to sources that sum to it, ties every source to an accepted
decision used exactly once, and reconciles the counters. Any mismatch is an
AuditError; a run is never published on top of one.
"""
from collections import Counter
from datetime import date
from fractions import Fraction
from .inputs import EventRow
from .invoice import Invoice
from .selection import Classification, TERMINAL_STATUSES


class AuditError(RuntimeError):
    """The outputs do not reconcile with their own trail; nothing may be published."""


def _require(condition, message):
    if not condition:
        raise AuditError(message)


def build_audit(rows: list[EventRow], classification: Classification, invoices: list[Invoice], usage: dict,
                period: dict) -> dict:
    decisions = []
    for row in rows:
        status = classification.statuses[row.line_number]
        if row.value is not None:
            event_id = row.value.get('event_id')
            event_id = event_id if isinstance(event_id, str) and event_id else None
        else:
            event_id = row.salvaged_identity.event_id if row.salvaged_identity else None
        decision = {'line': row.line_number, 'sha256': row.sha256, 'event_id': event_id, 'status': status}
        if status == 'quarantined':
            decision['reasons'] = list(classification.reasons[row.line_number])
        elif status == 'duplicate_ignored':
            decision['canonical_line'] = classification.duplicates[row.line_number]
        decisions.append(decision)
    trails = {}
    for inv in invoices:
        trails[inv.account_id] = {
            'currency': inv.currency, 'timezone': inv.timezone, 'period_end_plan_id': inv.subscription.period_end_plan_id,
            'subscription_lines': [
                {'plan_id': s.plan_id, 'from': s.start.isoformat(), 'to': s.end.isoformat(), 'days': s.days,
                 'days_in_period': period['days_in_period'], 'fee_minor': s.fee_minor, 'amount_minor': s.amount_minor,
                 'formula': f'round_half_up({s.fee_minor} * {s.days} / {period["days_in_period"]}) = {s.amount_minor}'}
                for s in inv.subscription.segments],
            'usage_lines': [
                {'metric': u.metric, 'plan_id': inv.subscription.period_end_plan_id, 'tier_from': u.tier_from, 'tier_to': u.tier_to,
                 'units': u.units, 'unit_price_micros': u.unit_price_micros, 'amount_minor': u.amount_minor,
                 'formula': f'round_half_up({u.units} * {u.unit_price_micros} / 10000) = {u.amount_minor}'}
                for u in inv.usage_lines],
            'usage_sources': {metric: [{'event_id': s['event_id'], 'line': s['line_number'], 'units': s['units']}
                                       for s in sorted(usage[inv.account_id][metric]['sources'], key=lambda s: s['line_number'])]
                              for metric, _ in inv.billable_units},
            'credit': {'credit_minor': inv.credit_minor, 'applied_minor': inv.credit_applied_minor, 'remaining_minor': inv.credit_remaining_minor},
            'quarantined_lines': list(inv.quarantined_lines),
        }
    return {'decisions': decisions, 'invoices': trails}


def _half_up(numerator: int, denominator: int) -> int:
    return int(Fraction(numerator, denominator) + Fraction(1, 2))


def reconcile(invoices: list[dict], quarantine: list[dict], audit: dict, manifest: dict) -> None:
    """Internal consistency, not raw-source authenticity. Bad document shapes are AuditError."""
    try:
        _require(isinstance(invoices, list) and isinstance(quarantine, list)
                 and isinstance(audit, dict) and isinstance(manifest, dict), 'invalid top-level document types')
        _require(isinstance(audit.get('decisions'), list) and isinstance(audit.get('invoices'), dict), 'invalid audit containers')
        _require(all(isinstance(manifest.get(key), dict) for key in
                     ('counts', 'totals_by_currency', 'quarantine_reasons', 'inputs', 'rules', 'versions')),
                 'invalid manifest containers')
        _require(all(isinstance(value, dict) for value in manifest['inputs'].values())
                 and isinstance(manifest['versions'].get('code_sha256'), dict)
                 and isinstance(manifest['rules'].get('metrics'), list), 'invalid nested manifest containers')
        required_inputs = {'events.jsonl', 'accounts.json', 'plans.json', 'period.json'}
        _require(set(manifest['inputs']) == required_inputs, 'manifest inputs must contain exactly the four required names')
        for name, metadata in manifest['inputs'].items():
            _require(set(metadata) == {'sha256', 'bytes', 'records'}, f'{name}: invalid input metadata fields')
            digest = metadata.get('sha256')
            _require(isinstance(digest, str) and len(digest) == 64
                     and all(c in '0123456789abcdef' for c in digest), f'{name}: invalid input sha256')
            for field in ('bytes', 'records'):
                value = metadata.get(field)
                _require(type(value) is int and value >= 0, f'{name}: invalid input {field}')
            _require(metadata['bytes'] > 0 or name == 'events.jsonl', f'{name}: empty configuration input')
            _require(metadata['records'] > 0 or name == 'events.jsonl', f'{name}: empty configuration records')
            if name == 'period.json':
                _require(metadata['records'] == 1, 'period.json: records must be 1')
        _require(manifest['inputs']['events.jsonl']['records'] == manifest['counts'].get('raw'),
                 'events.jsonl: records differ from raw count')
        _require(manifest['inputs']['accounts.json']['records'] == len(invoices),
                 'accounts.json: records differ from invoice count')
        _reconcile(invoices, quarantine, audit, manifest)
    except (KeyError, TypeError, AttributeError, ValueError, IndexError, OverflowError, ZeroDivisionError, RecursionError) as exc:
        raise AuditError('malformed output document: missing field, invalid type or arithmetic value') from exc


def _reconcile(invoices: list[dict], quarantine: list[dict], audit: dict, manifest: dict) -> None:
    """Independent invariant check over the published documents; raises AuditError on the first inconsistency."""
    decisions = audit['decisions']
    counts = manifest['counts']
    _require([d['line'] for d in decisions] == list(range(1, len(decisions) + 1)), 'decisions must cover raw lines 1..n exactly once')
    _require(counts['raw'] == len(decisions), 'manifest raw count differs from the number of decisions')
    by_status = Counter(d['status'] for d in decisions)
    _require(set(by_status) <= set(TERMINAL_STATUSES), 'unknown terminal status in decisions')
    for status in TERMINAL_STATUSES:
        _require(counts.get(status, 0) == by_status.get(status, 0), f'manifest count for {status} does not match decisions')
    _require(sum(by_status.values()) == counts['raw'], 'terminal counts do not reconcile with the raw count')
    quarantined = [d for d in decisions if d['status'] == 'quarantined']
    _require(len(quarantine) == len(quarantined) == counts['quarantine_entries'], 'quarantine.json size differs from quarantined decisions')
    for entry, decision in zip(quarantine, quarantined):
        _require(entry == {'event_id': decision['event_id'], 'reason': decision['reasons'][0]}, f'quarantine entry for line {decision["line"]} differs from its decision')
    _require(manifest['quarantine_reasons'] == dict(sorted(Counter(d['reasons'][0] for d in quarantined).items())), 'quarantine reason counts differ')

    decision_by_line = {d['line']: d for d in decisions}
    for decision in decisions:
        digest = decision['sha256']
        _require(isinstance(digest, str) and len(digest) == 64 and all(c in '0123456789abcdef' for c in digest),
                 'invalid source hash representation (raw source verification is a separate gate)')
        if decision['status'] == 'duplicate_ignored':
            target = decision_by_line.get(decision['canonical_line'])
            _require(target is not None and target is not decision and target['status'] != 'duplicate_ignored'
                     and decision['event_id'] is not None and target['event_id'] == decision['event_id'],
                     'duplicate target must be a canonical decision with the same identity')
    used_sources = Counter()
    _require([inv['account_id'] for inv in invoices] == sorted(inv['account_id'] for inv in invoices), 'invoices are not sorted by account_id')
    _require(counts['invoices'] == len(invoices) == len(audit['invoices']), 'invoice count differs from manifest or trail')
    totals = {}
    subscription_lines = usage_lines = 0
    for inv in invoices:
        trail = audit['invoices'].get(inv['account_id'])
        _require(trail is not None, f'{inv["account_id"]}: no audit trail')
        where = inv['account_id']
        _require(inv['currency'] == trail['currency'] and inv['timezone'] == trail['timezone'], f'{where}: currency or timezone differs from the trail')
        expected_lines = []
        for seg in trail['subscription_lines']:
            days = (date.fromisoformat(seg['to']) - date.fromisoformat(seg['from'])).days
            _require(days == seg['days'] and 0 < days <= seg['days_in_period'], f'{where}: segment days do not match its dates')
            _require(seg['amount_minor'] == _half_up(seg['fee_minor'] * seg['days'], seg['days_in_period']), f'{where}: subscription amount does not recompute')
            _require(seg['formula'] == f"round_half_up({seg['fee_minor']} * {seg['days']} / {seg['days_in_period']}) = {seg['amount_minor']}",
                     f'{where}: subscription formula differs from arithmetic')
            expected_lines.append({'kind': 'subscription', 'plan_id': seg['plan_id'], 'days': seg['days'], 'amount_minor': seg['amount_minor']})
        for metric, units in inv['billable_units'].items():
            sources = trail['usage_sources'].get(metric)
            _require(sources is not None, f'{where}/{metric}: no sources in the trail')
            _require(sum(s['units'] for s in sources) == units, f'{where}/{metric}: source units do not sum to billable_units')
            for source in sources:
                decision = decision_by_line.get(source['line'])
                _require(decision is not None and decision['status'] == 'accepted' and decision['event_id'] == source['event_id']
                         and type(source['units']) is int and source['units'] > 0, f'{where}/{metric}: source line {source["line"]} is not an accepted event')
                used_sources[source['line']] += 1
            tier_lines = [u for u in trail['usage_lines'] if u['metric'] == metric]
            _require(sum(u['units'] for u in tier_lines) == units, f'{where}/{metric}: tier units do not sum to billable_units')
            position = 0
            for u in tier_lines:
                _require(u['tier_from'] == position and (u['tier_to'] is None or u['tier_to'] > u['tier_from']), f'{where}/{metric}: tiers are not contiguous from 0')
                _require(u['plan_id'] == trail['period_end_plan_id'], f'{where}/{metric}: usage priced on a plan other than the period-end plan')
                _require(u['units'] > 0 and (u['tier_to'] is None or u['units'] <= u['tier_to'] - u['tier_from']), f'{where}/{metric}: tier units exceed the bracket')
                expected_units = max(0, (units if u['tier_to'] is None else min(units, u['tier_to'])) - u['tier_from'])
                _require(type(u['units']) is int and u['units'] == expected_units and expected_units > 0,
                         f'{where}/{metric}: tier allocation is not the expected cumulative split')
                _require(u['amount_minor'] == _half_up(u['units'] * u['unit_price_micros'], 10000), f'{where}/{metric}: usage amount does not recompute')
                _require(u['formula'] == f"round_half_up({u['units']} * {u['unit_price_micros']} / 10000) = {u['amount_minor']}",
                         f'{where}/{metric}: usage formula differs from arithmetic')
                position = u['tier_to']
                expected_lines.append({'kind': 'usage', 'metric': metric, 'tier_from': u['tier_from'], 'tier_to': u['tier_to'],
                                       'units': u['units'], 'amount_minor': u['amount_minor']})
        _require(inv['lines'] == expected_lines, f'{where}: invoice lines differ from the trail')
        subscription_lines += len(trail['subscription_lines'])
        usage_lines += len(trail['usage_lines'])
        subtotal = sum(line['amount_minor'] for line in inv['lines'])
        credit = trail['credit']
        applied = min(subtotal, credit['credit_minor'])
        _require(inv['subtotal_minor'] == subtotal, f'{where}: subtotal is not the sum of the lines')
        _require(inv['credit_applied_minor'] == applied == credit['applied_minor'], f'{where}: credit applied is not min(subtotal, credit)')
        _require(inv['credit_remaining_minor'] == credit['credit_minor'] - applied == credit['remaining_minor'], f'{where}: credit remaining does not reconcile')
        _require(inv['total_minor'] == subtotal - applied and inv['total_minor'] >= 0, f'{where}: total is not subtotal minus credit, or is negative')
        _require(inv['quarantined_count'] == len(trail['quarantined_lines']), f'{where}: quarantined_count differs from the trail')
        for line in trail['quarantined_lines']:
            _require(decision_by_line.get(line, {}).get('status') == 'quarantined', f'{where}: quarantined line {line} is not a quarantined decision')
        bucket = totals.setdefault(inv['currency'], {'subtotal_minor': 0, 'credit_applied_minor': 0, 'total_minor': 0, 'invoices': 0})
        bucket['subtotal_minor'] += subtotal
        bucket['credit_applied_minor'] += applied
        bucket['total_minor'] += inv['total_minor']
        bucket['invoices'] += 1
    accepted_lines = {d['line'] for d in decisions if d['status'] == 'accepted'}
    _require(set(used_sources) == accepted_lines and all(n == 1 for n in used_sources.values()),
             'every accepted event must be a source of exactly one invoice metric')
    _require(counts['subscription_lines'] == subscription_lines and counts['usage_lines'] == usage_lines, 'manifest line counts differ from the trail')
    _require(manifest['totals_by_currency'] == dict(sorted(totals.items())), 'manifest totals by currency do not reconcile')
