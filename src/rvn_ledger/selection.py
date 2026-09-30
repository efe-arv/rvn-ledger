"""Record disposition: canonical-copy selection, then the shared classification glue.

Deduplication runs BEFORE any business validation. Rows without a readable
identity/sequence cannot participate in ordering and are quarantined. A
strictly rejected line whose identity was salvaged by `inputs` does take part
in ordering: if it is the first copy by `ingest_seq` the event is quarantined
with its parse reason and later copies are `duplicate_ignored`; a later
rejected copy is simply `duplicate_ignored`. A valid copy never replaces a
broken first copy. Equal sequences use physical line order as an explicit
fallback assumption; this unspecified edge case is documented, not a source rule.

`classify_events` is the single product function that gives every raw line
exactly one terminal status (architecture section 6). Configuration is
resolved eagerly for every account before any event is examined, so an
unusable account or period fails the run even when no event references it.
"""
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from .inputs import EventRow, InputError
from .timing import account_bounds, classify_time
from .validation import validate_event

TERMINAL_STATUSES = ('accepted', 'duplicate_ignored', 'excluded_out_of_period', 'excluded_late', 'quarantined')


@dataclass(frozen=True)
class Deduplication:
    winners: list[EventRow]      # parsed canonical copies, in source line order
    duplicates: dict[int, int]   # ignored source line -> canonical source line
    rejected: dict[int, str]     # source line -> reason (includes unreadable canonical copies)


def deduplicate(rows: list[EventRow]) -> Deduplication:
    canonical = {}   # event_id -> (ingest_seq, line_number)
    candidates = []  # (row, event_id, ingest_seq)
    rejected = {}
    for row in rows:
        if row.value is None:
            if row.salvaged_identity is None:
                rejected[row.line_number] = row.error
                continue
            event_id, sequence = row.salvaged_identity.event_id, row.salvaged_identity.ingest_seq
        else:
            event_id = row.value.get('event_id')
            sequence = row.value.get('ingest_seq')
            if not isinstance(event_id, str) or not event_id:
                rejected[row.line_number] = 'invalid_event_id'
                continue
            if type(sequence) is not int:
                rejected[row.line_number] = 'invalid_ingest_seq'
                continue
        candidates.append((row, event_id, sequence))
        key = (sequence, row.line_number)
        if event_id not in canonical or key < canonical[event_id]:
            canonical[event_id] = key
    winners, duplicates = [], {}
    for row, event_id, _ in candidates:
        winner_line = canonical[event_id][1]
        if row.line_number != winner_line:
            duplicates[row.line_number] = winner_line
        elif row.value is None:
            rejected[row.line_number] = row.error  # broken first copy: quarantined, never replaced
        else:
            winners.append(row)
    return Deduplication(winners, duplicates, rejected)


@dataclass(frozen=True)
class AcceptedEvent:
    """A validated, time-accepted deduplication winner; the only input aggregation takes."""
    row: EventRow
    ts: datetime
    ingested_at: datetime


@dataclass(frozen=True)
class Classification:
    statuses: dict[int, str]              # every raw line -> exactly one terminal status
    reasons: dict[int, tuple[str, ...]]   # quarantined line -> ordered reasons; first is primary
    duplicates: dict[int, int]            # ignored line -> canonical line
    accepted: list[AcceptedEvent]         # in source line order
    counts: dict[str, int]                # 'raw' plus one entry per status that occurred


def _metrics(period) -> set[str]:
    metrics = period.get('metrics') if isinstance(period, dict) else None
    if not isinstance(metrics, list) or not metrics:
        raise InputError('period.metrics must be a nonempty list')
    if any(not isinstance(metric, str) or not metric for metric in metrics):
        raise InputError('period.metrics entries must be nonempty strings')
    if len(set(metrics)) != len(metrics):
        raise InputError('period.metrics must not repeat a metric')
    return set(metrics)


def _assign(statuses: dict[int, str], line: int, status: str) -> None:
    """A line reported twice by deduplication is a defect, never a silent overwrite."""
    if line in statuses:
        raise RuntimeError(f'every raw line must receive exactly one terminal status; line {line} got a second one')
    statuses[line] = status


def classify_events(rows: list[EventRow], accounts: list, period: dict) -> Classification:
    """deduplicate -> validate -> time filter, with eager configuration checks first."""
    bounds = account_bounds(accounts, period)
    metrics = _metrics(period)
    selected = deduplicate(rows)
    statuses, reasons, accepted = {}, {}, []
    for line, reason in selected.rejected.items():
        _assign(statuses, line, 'quarantined')
        reasons[line] = (reason,)
    for line in selected.duplicates:
        _assign(statuses, line, 'duplicate_ignored')
    account_ids = set(bounds)
    for row in selected.winners:
        event = validate_event(row, account_ids, metrics)
        if event.reasons:
            status = 'quarantined'
            reasons[row.line_number] = event.reasons
        else:
            status = classify_time(event, bounds[row.value['account_id']])
            if status == 'accepted':
                accepted.append(AcceptedEvent(row, event.ts, event.ingested_at))
        _assign(statuses, row.line_number, status)
    expected_lines = [row.line_number for row in rows]
    if sorted(statuses) != sorted(expected_lines) or len(expected_lines) != len(set(expected_lines)):
        raise RuntimeError('every raw line must receive exactly one terminal status; a line was left without one')
    if any(status not in TERMINAL_STATUSES for status in statuses.values()):
        raise RuntimeError('unknown terminal status')
    counts = {'raw': len(rows), **dict(sorted(Counter(statuses.values()).items()))}
    return Classification(statuses, reasons, selected.duplicates, accepted, counts)
