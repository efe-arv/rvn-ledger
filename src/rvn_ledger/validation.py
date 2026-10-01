"""Validate canonical events; timestamps keep exact fractional seconds.

Timestamp policy: extended ISO date/time with seconds, Z or an explicit HH:MM
UTC offset. Naive times, leap seconds and fractional offsets are refused. A
fraction may have up to MAX_INTEGER_DIGITS (256) digits. That is below the
smallest int-string limit Python allows (640), so whether a timestamp parses
never depends on PYTHONINTMAXSTRDIGITS or sys.set_int_max_str_digits.
Boundary decisions use exact rational seconds, never datetime's truncated fraction.
Reason precedence: account, metric, units, usage timestamp, ingestion timestamp.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from fractions import Fraction
import re
from .inputs import EventRow, MAX_INTEGER_DIGITS


@dataclass(frozen=True)
class ValidatedEvent:
    row: EventRow
    reasons: tuple[str, ...]
    ts: datetime | None
    ingested_at: datetime | None
    ts_exact: Fraction | None = None
    ingested_at_exact: Fraction | None = None


_TIMESTAMP = re.compile(r'(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:[.,](\d+))?(Z|[+-]\d{2}:\d{2})', re.ASCII)
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def utc_seconds(value: datetime) -> Fraction:
    delta = value.astimezone(timezone.utc) - _EPOCH
    return Fraction((delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds, 1000000)


def _timestamp_parts(value):
    match = _TIMESTAMP.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        return None, None
    base, digits, offset = match.groups()
    if digits is not None and len(digits) > MAX_INTEGER_DIGITS:
        return None, None
    try:
        if offset != 'Z' and (int(offset[1:3]) >= 24 or int(offset[4:6]) >= 60):
            return None, None
        parsed = datetime.fromisoformat(base + offset.replace('Z', '+00:00')).astimezone(timezone.utc)
        fraction = Fraction(int(digits), 10 ** len(digits)) if digits else Fraction(0)
        exact = utc_seconds(parsed) + fraction
        # datetime is a compatibility view, NOT the value used for boundary comparisons.
        return parsed.replace(microsecond=int(fraction * 1000000)), exact
    except (ValueError, OverflowError):
        return None, None


def _timestamp(value):
    return _timestamp_parts(value)[0]


def validate_event(row: EventRow, account_ids: set[str], metrics: set[str]) -> ValidatedEvent:
    """Call only on deduplication winners; never validate a replacement copy."""
    if row.error is not None or row.value is None:
        raise ValueError('validate_event requires a parsed deduplication winner')
    value = row.value
    reasons = []
    account = value.get('account_id')
    metric = value.get('metric')
    if not isinstance(account, str) or account not in account_ids:
        reasons.append('unknown_account')
    if not isinstance(metric, str) or metric not in metrics:
        reasons.append('unknown_metric')
    units = value.get('units')
    if type(units) is not int:
        reasons.append('invalid_units')
    elif units <= 0:
        reasons.append('nonpositive_units')
    ts, ts_exact = _timestamp_parts(value.get('ts'))
    ingested_at, ingestion_exact = _timestamp_parts(value.get('ingested_at'))
    if ts is None:
        reasons.append('invalid_ts')
    if ingested_at is None:
        reasons.append('invalid_ingested_at')
    return ValidatedEvent(row, tuple(reasons), ts, ingested_at, ts_exact, ingestion_exact)
