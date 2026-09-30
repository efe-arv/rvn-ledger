"""Exact accepted-event totals with stable, lossless source references.

Only `AcceptedEvent` values (validated, time-accepted deduplication winners
produced by `selection.classify_events`) cross this boundary; a plain row is a
TypeError, because aggregation cannot re-derive time acceptance itself.
No pricing or invoice construction happens here.
"""
from .selection import AcceptedEvent


def _ids(values, what: str) -> list[str]:
    """Read an id collection exactly once and refuse anything that would collapse silently."""
    if isinstance(values, (str, bytes, dict)) or not hasattr(values, '__iter__'):
        raise ValueError(f'{what} must be a collection of nonempty strings')
    ids = list(values)  # one pass: an iterator read per account would leave later accounts empty
    if any(not isinstance(value, str) or not value for value in ids):
        raise ValueError(f'{what} must contain only nonempty strings')
    if len(set(ids)) != len(ids):
        raise ValueError(f'{what} must not repeat an id')
    return sorted(ids)


def aggregate_usage(events: list[AcceptedEvent], account_ids, metrics) -> dict:
    accounts, metric_ids = _ids(account_ids, 'account_ids'), _ids(metrics, 'metrics')
    result = {a: {m: {'units': 0, 'sources': []} for m in metric_ids} for a in accounts}
    seen = set()
    for event in events:
        if not isinstance(event, AcceptedEvent):
            raise TypeError('aggregation accepts only AcceptedEvent values from selection.classify_events')
        row = event.row
        value = row.value
        if row.error or not isinstance(value, dict):
            raise ValueError('aggregation requires accepted event rows')
        account, metric, units = value.get('account_id'), value.get('metric'), value.get('units')
        event_id = value.get('event_id')
        if not isinstance(account, str) or account not in result or not isinstance(metric, str) or metric not in result[account]:
            raise ValueError('unknown account or metric')
        if type(units) is not int or units <= 0:
            raise ValueError('units must be a positive integer')
        if not isinstance(event_id, str) or not event_id or event_id in seen:
            raise ValueError('missing or duplicate event identity')
        seen.add(event_id)
        group = result[account][metric]
        group['units'] += units
        group['sources'].append({'event_id': event_id, 'line_number': row.line_number, 'sha256': row.sha256, 'units': units})
    for groups in result.values():
        for group in groups.values():
            group['sources'].sort(key=lambda source: (source['event_id'], source['line_number']))
    return result
