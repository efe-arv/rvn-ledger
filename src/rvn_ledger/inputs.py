"""Strict configuration parsing and lossless per-line event ingestion.

Event business validation belongs to selection, not this module. Every physical
line (including blank lines) receives a source reference and a parse outcome.

Strictness policy: NaN/Infinity tokens, floats that overflow to infinity,
repeated object keys, over-long integers and over-deep nesting are all
rejected. A rejected line never yields a payload. When such a line still
carries an unambiguous `event_id`/`ingest_seq`, that identity alone is
salvaged so the line can take part in deduplication ordering (spec rule 2:
the first copy by `ingest_seq` wins, later copies are ignored). Lines whose
identity cannot be read unambiguously (truncated JSON, invalid UTF-8, repeated
identity keys in the top-level object) keep no identity; that remainder is an
OPEN contract decision recorded in the architecture document, not a guess.
Repeated keys inside nested objects still reject the line but do not make its
own identity ambiguous.
"""
from dataclasses import dataclass
from datetime import date
import hashlib
import json
import math

# Products of admitted integers plus aggregate growth fit below Python's minimum
# configurable decimal-conversion limit (640 digits), without changing it globally.
MAX_INTEGER_DIGITS = 256


class InputError(ValueError):
    """An unusable reference configuration; stop before billing."""


class IntegerTooLarge(ValueError):
    """A JSON integer exceeds the supported decimal input size."""


def _bounded_int(text: str) -> int:
    if len(text.lstrip('-')) > MAX_INTEGER_DIGITS:
        raise IntegerTooLarge(f'JSON integers support at most {MAX_INTEGER_DIGITS} decimal digits')
    return int(text)


def period_metrics(period) -> tuple[str, ...]:
    """Validate metric identities while preserving their configured order."""
    metrics = period.get('metrics') if isinstance(period, dict) else None
    if not isinstance(metrics, list) or not metrics:
        raise InputError('period.metrics must be a nonempty list')
    if any(not isinstance(metric, str) or not metric for metric in metrics):
        raise InputError('period.metrics entries must be nonempty strings')
    if len(set(metrics)) != len(metrics):
        raise InputError('period.metrics must not repeat a metric')
    return tuple(metrics)


def local_date(value, what: str) -> date:
    """The one strict local-date parser for configuration: canonical YYYY-MM-DD only.

    `date.fromisoformat` on CPython 3.11 also accepts '20260901' and '2026-W36-2';
    every module that reads a period or plan-segment date must refuse those the
    same way (review F3), so none of them calls fromisoformat directly.
    """
    if not isinstance(value, str):
        raise InputError(f'{what} must be a YYYY-MM-DD local date')
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise InputError(f'{what} must be a YYYY-MM-DD local date') from exc
    if parsed.isoformat() != value:
        raise InputError(f'{what} must be a YYYY-MM-DD local date')
    return parsed


_IDENTITY_KEYS = ('event_id', 'ingest_seq')
_PARSE_FAILURES = (ValueError, RecursionError)


def _reject_constant(value):
    raise ValueError('non-finite JSON number')


def _finite_float(text):
    value = float(text)
    if not math.isfinite(value):
        raise ValueError('non-finite JSON number')
    return value


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


class InvalidUnicode(ValueError):
    """JSON contains a lone surrogate, not a Unicode scalar value."""


def _unicode_scalars(node):
    if isinstance(node, str):
        if any(0xD800 <= ord(char) <= 0xDFFF for char in node):
            raise InvalidUnicode('unpaired Unicode surrogate')
    elif isinstance(node, dict):
        for key, value in node.items():
            _unicode_scalars(key)
            _unicode_scalars(value)
    elif isinstance(node, list):
        for value in node:
            _unicode_scalars(value)


def _parse(raw):
    value = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_object,
                       parse_constant=_reject_constant, parse_float=_finite_float, parse_int=_bounded_int)
    _unicode_scalars(value)
    return value


def read_json(raw: bytes, name: str):
    """Return parsed configuration plus the hash of the exact input bytes."""
    try:
        value = _parse(raw)
    except IntegerTooLarge as exc:
        raise InputError(f'{name}: {exc}') from exc
    except (UnicodeDecodeError, *_PARSE_FAILURES) as exc:
        raise InputError(f'{name}: invalid UTF-8 or JSON') from exc
    return value, hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class Identity:
    event_id: str
    ingest_seq: int


@dataclass(frozen=True)
class EventRow:
    line_number: int
    sha256: str
    value: dict | None
    error: str | None
    # Set only for strictly rejected lines whose identity was readable without
    # ambiguity. Parsed rows carry their identity inside `value`.
    salvaged_identity: Identity | None = None


def _recover_identity(line: bytes) -> Identity | None:
    """Identity-only salvage for a strictly rejected line; never yields a payload."""
    top_level_keys = []

    def pairs(items):
        # The decoder hooks nested objects before their parent, so the last call sees
        # the top-level object. Only repetition of the line's OWN identity keys is
        # ambiguous; a repeated key inside a nested object says nothing about them.
        top_level_keys[:] = [key for key, _ in items]
        return dict(items)

    def bounded_int(text):
        # Over-long integers become an opaque marker instead of aborting the salvage.
        return int(text) if len(text.lstrip('-')) <= MAX_INTEGER_DIGITS else object()

    try:
        value = json.loads(line.decode('utf-8'), object_pairs_hook=pairs, parse_int=bounded_int)
    except (UnicodeDecodeError, *_PARSE_FAILURES):
        return None
    if not isinstance(value, dict) or any(top_level_keys.count(key) > 1 for key in _IDENTITY_KEYS):
        return None
    event_id, sequence = value.get('event_id'), value.get('ingest_seq')
    if not isinstance(event_id, str) or not event_id or type(sequence) is not int:
        return None
    return Identity(event_id, sequence)


UTF8_BOM = b'\xef\xbb\xbf'


def read_events(raw: bytes) -> list[EventRow]:
    """Every physical line, hashed exactly as written.

    A UTF-8 byte order mark at the start of the file belongs to the file's encoding, not to its
    first record: it is removed before the first line is parsed or its identity salvaged, so the
    first copy of an event does not lose deduplication to a later copy over an editor artefact.
    The line's SHA-256 and the manifest's file hash still cover the BOM bytes. A BOM anywhere
    else is not a file mark and is rejected like any other stray bytes.
    """
    rows = []
    # Split only at LF, not at arbitrary Unicode or control characters.
    lines = raw.split(b'\n')
    if lines[-1] == b'':
        lines.pop()
    for index, line in enumerate(lines, 1):
        source = line + (b'\n' if index < len(lines) or raw.endswith(b'\n') else b'')
        digest = hashlib.sha256(source).hexdigest()
        if index == 1 and line.startswith(UTF8_BOM):
            line = line[len(UTF8_BOM):]
        try:
            value = _parse(line)
        except UnicodeDecodeError:
            rows.append(EventRow(index, digest, None, 'invalid_utf8'))
        except InvalidUnicode:
            rows.append(EventRow(index, digest, None, 'invalid_unicode', _recover_identity(line)))
        except IntegerTooLarge:
            rows.append(EventRow(index, digest, None, 'integer_too_large', _recover_identity(line)))
        except _PARSE_FAILURES:
            # RecursionError from over-deep nesting is quarantined, never a crash.
            rows.append(EventRow(index, digest, None, 'invalid_json', _recover_identity(line)))
        else:
            if not isinstance(value, dict):
                rows.append(EventRow(index, digest, None, 'not_object'))
            else:
                rows.append(EventRow(index, digest, value, None))
    return rows
