"""Audit trail (every raw line's fate, every invoice line's sources) and its independent reconciliation.

`build_audit` records, per raw line, the terminal status with the line's SHA-256
and identity, and per invoice the plan segments, the tier lines with their unit
price and formula, the accepted source events per metric, the credit arithmetic
and the attributed quarantined lines.

`reconcile` is the invariant check the CLI runs before publishing and that
`rvn-ledger check` re-runs on published files. It does two kinds of checks:

- Rule-independent invariants: every raw line has exactly one decision, every
  event identity has exactly one canonical line, every accepted event is the
  source of exactly one invoice metric, source units sum to billable units,
  tier units sum to billable units, subtotal is the sum of the lines, total =
  subtotal - credit applied and is never negative, credit applied + remaining
  = credit, and every counter in the manifest matches.
- Period consistency (external review F1): the manifest rules are a valid
  billing period read through the same strict parsers as period.json; every
  subscription segment lies inside it, uses its denominator, is chronological
  and non-overlapping; the period-end plan is the segment covering the final
  local day and is the plan every usage line was priced on; billable_units
  list the manifest metrics in their order.
- Provenance consistency (external review F3): the manifest names the TZif
  source and hash of exactly the zones the invoices were billed in.
- Recomputation from the recorded pricing inputs (fee, days, tariff, credit)
  through the SAME rule functions the invoice builder uses (`money`, `tiers`).
  The trail must reproduce the published lines exactly.

The billing rules themselves are therefore written once: changing a rule in
`money.py` or `tiers.py` changes the invoices and this check together. Whether
the rules are the right rules is the job of the hand-derived tests.
Any mismatch is an AuditError; a run is never published on top of one.
"""
from collections import Counter
from datetime import date, timedelta
from .inputs import EventRow, InputError, local_date, period_metrics
from .invoice import Invoice
from .money import apply_credit, subscription_amount, subscription_formula, usage_formula
from .selection import Classification, TERMINAL_STATUSES
from .subscription import period_days
from .tiers import price_usage, tiers_from_document
from .timing import ZONE_SOURCES, provenance_summary


class AuditError(RuntimeError):
    """The outputs do not reconcile with their own trail; nothing may be published."""


def _require(condition, message):
    if not condition:
        raise AuditError(message)


def _sha256_text(value) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


def _local_date(value, what: str) -> date:
    try:
        return local_date(value, what)   # the one strict YYYY-MM-DD parser, shared with the run
    except InputError as exc:
        raise AuditError(str(exc)) from exc


def _manifest_period(rules: dict) -> tuple[date, date, int, list]:
    """The manifest's billing period, read through the same validators as period.json (review F1)."""
    try:
        start, end, days = period_days(rules)
        metrics = list(period_metrics(rules))
    except InputError as exc:
        raise AuditError(f'manifest rules do not describe a valid billing period: {exc}') from exc
    late = rules.get('late_cutoff_hours_after_period_end')
    _require(type(late) is int and late >= 0, 'manifest late cutoff must be a nonnegative integer of hours')
    _require(isinstance(rules.get('source'), str) and rules['source'], 'manifest rules must name their source')
    return start, end, days, metrics


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
                 'formula': subscription_formula(s.fee_minor, s.days, period['days_in_period'], s.amount_minor)}
                for s in inv.subscription.segments],
            'usage_tariffs': {
                metric: {'plan_id': inv.subscription.period_end_plan_id,
                         'tiers': [{'from_units': t.from_units, 'to_units': t.to_units, 'unit_price_micros': t.unit_price_micros}
                                   for t in tiers]}
                for metric, tiers in inv.tariffs},
            'usage_lines': [
                {'metric': u.metric, 'plan_id': inv.subscription.period_end_plan_id, 'tier_from': u.tier_from, 'tier_to': u.tier_to,
                 'units': u.units, 'unit_price_micros': u.unit_price_micros, 'amount_minor': u.amount_minor,
                 'formula': usage_formula(u.units, u.unit_price_micros, u.amount_minor)}
                for u in inv.usage_lines],
            'usage_sources': {metric: [{'event_id': s['event_id'], 'line': s['line_number'], 'units': s['units']}
                                       for s in sorted(usage[inv.account_id][metric]['sources'], key=lambda s: s['line_number'])]
                              for metric, _ in inv.billable_units},
            'credit': {'credit_minor': inv.credit_minor, 'applied_minor': inv.credit_applied_minor, 'remaining_minor': inv.credit_remaining_minor},
            'quarantined_lines': list(inv.quarantined_lines),
        }
    return {'decisions': decisions, 'invoices': trails}


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
            _require(_sha256_text(metadata.get('sha256')), f'{name}: invalid input sha256')
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
    period_start, period_end, days_in_period, metrics = _manifest_period(manifest['rules'])
    last_day = period_end - timedelta(days=1)
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
    canonical = {}   # event_id -> canonical line, over every decision that took part in deduplication (review F2)
    for decision in decisions:
        _require(_sha256_text(decision['sha256']), 'invalid source hash representation (raw source verification is a separate gate)')
        event_id = decision['event_id']
        if decision['status'] == 'duplicate_ignored':
            target = decision_by_line.get(decision['canonical_line'])
            _require(target is not None and target is not decision and target['status'] != 'duplicate_ignored'
                     and isinstance(event_id, str) and event_id and target['event_id'] == event_id,
                     'duplicate target must be a canonical decision with the same identity')
            continue
        if decision['status'] == 'accepted':
            _require(isinstance(event_id, str) and event_id, f'line {decision["line"]}: an accepted event must carry a nonempty string identity')
        if event_id is None or (decision['status'] == 'quarantined' and decision['reasons'][0] == 'invalid_ingest_seq'):
            continue   # no readable identity, or a readable identity whose sequence kept it out of deduplication
        _require(isinstance(event_id, str) and event_id, f'line {decision["line"]}: identity must be a nonempty string')
        _require(event_id not in canonical, f'line {decision["line"]}: event identity already has canonical line {canonical.get(event_id)}')
        canonical[event_id] = decision['line']
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
        end_plan = trail['period_end_plan_id']
        _require(isinstance(end_plan, str) and end_plan, f'{where}: period_end_plan_id must be a nonempty string')
        expected_lines = []
        previous_end, covering = None, None
        for seg in trail['subscription_lines']:
            start, end = _local_date(seg['from'], f'{where}: segment from'), _local_date(seg['to'], f'{where}: segment to')
            days = (end - start).days
            _require(days == seg['days'] and 0 < days <= seg['days_in_period'], f'{where}: segment days do not match its dates')
            _require(seg['days_in_period'] == days_in_period, f'{where}: segment denominator differs from the manifest days_in_period')
            _require(period_start <= start and end <= period_end, f'{where}: subscription segment lies outside the manifest period')
            _require(previous_end is None or previous_end <= start, f'{where}: subscription segments overlap or are out of order')
            _require(isinstance(seg['plan_id'], str) and seg['plan_id'], f'{where}: segment plan_id must be a nonempty string')
            previous_end = end
            if start <= last_day < end:
                covering = seg['plan_id']
            _require(seg['amount_minor'] == subscription_amount(seg['fee_minor'], seg['days'], seg['days_in_period']),
                     f'{where}: subscription amount does not recompute')
            _require(seg['formula'] == subscription_formula(seg['fee_minor'], seg['days'], seg['days_in_period'], seg['amount_minor']),
                     f'{where}: subscription formula differs from arithmetic')
            expected_lines.append({'kind': 'subscription', 'plan_id': seg['plan_id'], 'days': seg['days'], 'amount_minor': seg['amount_minor']})
        _require(covering == end_plan, f'{where}: period_end_plan_id is not the plan covering the final local day of the period')
        _require(list(inv['billable_units']) == metrics, f'{where}: billable_units do not list the manifest metrics in their order')
        _require(set(trail['usage_sources']) == set(metrics) and set(trail['usage_tariffs']) == set(metrics)
                 and all(u['metric'] in metrics for u in trail['usage_lines']), f'{where}: trail metrics differ from the manifest')
        for metric, units in inv['billable_units'].items():
            sources = trail['usage_sources'][metric]
            _require(sum(s['units'] for s in sources) == units, f'{where}/{metric}: source units do not sum to billable_units')
            for source in sources:
                decision = decision_by_line.get(source['line'])
                _require(decision is not None and decision['status'] == 'accepted' and decision['event_id'] == source['event_id']
                         and type(source['units']) is int and source['units'] > 0, f'{where}/{metric}: source line {source["line"]} is not an accepted event')
                used_sources[source['line']] += 1
            tier_lines = [u for u in trail['usage_lines'] if u['metric'] == metric]
            _require(sum(u['units'] for u in tier_lines) == units, f'{where}/{metric}: tier units do not sum to billable_units')
            tariff = trail['usage_tariffs'][metric]
            _require(tariff['plan_id'] == end_plan, f'{where}/{metric}: usage priced on a plan other than the period-end plan')
            recomputed = [{'metric': line.metric, 'plan_id': tariff['plan_id'], 'tier_from': line.tier_from, 'tier_to': line.tier_to,
                           'units': line.units, 'unit_price_micros': line.unit_price_micros, 'amount_minor': line.amount_minor,
                           'formula': usage_formula(line.units, line.unit_price_micros, line.amount_minor)}
                          for line in price_usage(units, tiers_from_document(tariff['tiers']), metric)]
            _require(tier_lines == recomputed, f'{where}/{metric}: usage lines do not recompute from the recorded tariff')
            for u in tier_lines:
                expected_lines.append({'kind': 'usage', 'metric': metric, 'tier_from': u['tier_from'], 'tier_to': u['tier_to'],
                                       'units': u['units'], 'amount_minor': u['amount_minor']})
        _require(inv['lines'] == expected_lines, f'{where}: invoice lines differ from the trail')
        subscription_lines += len(trail['subscription_lines'])
        usage_lines += len(trail['usage_lines'])
        credit = trail['credit']
        subtotal, applied, remaining, total = apply_credit([line['amount_minor'] for line in inv['lines']], credit['credit_minor'])
        # Invariants that hold whatever the credit rule is:
        _require(inv['subtotal_minor'] == sum(line['amount_minor'] for line in inv['lines']), f'{where}: subtotal is not the sum of the lines')
        _require(inv['total_minor'] == inv['subtotal_minor'] - inv['credit_applied_minor'] and inv['total_minor'] >= 0,
                 f'{where}: total is not subtotal minus credit applied, or is negative')
        _require(inv['credit_applied_minor'] + inv['credit_remaining_minor'] == credit['credit_minor'], f'{where}: credit is not conserved')
        # The credit rule itself, recomputed by money.apply_credit:
        _require((inv['subtotal_minor'], inv['credit_applied_minor'], inv['credit_remaining_minor'], inv['total_minor'])
                 == (subtotal, applied, remaining, total), f'{where}: credit does not recompute')
        _require((credit['applied_minor'], credit['remaining_minor']) == (applied, remaining), f'{where}: credit trail differs from the invoice')
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
    provenance = manifest['versions'].get('timezones')
    _require(isinstance(provenance, dict) and list(provenance) == sorted({inv['timezone'] for inv in invoices}),
             'manifest timezone provenance must name exactly the invoiced zones in sorted order')
    for key, record in provenance.items():
        _require(isinstance(record, dict) and set(record) == {'source', 'version', 'sha256'} and record['source'] in ZONE_SOURCES
                 and isinstance(record['version'], str), f'{key}: invalid timezone provenance record')
        _require(record['sha256'] is None if record['source'] == 'unknown' else _sha256_text(record['sha256']),
                 f'{key}: timezone provenance hash does not fit its source kind')
    _require(manifest['versions']['tzdata'] == provenance_summary(provenance), 'manifest tzdata summary differs from the timezone provenance')
