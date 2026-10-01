"""Read-only diagnostics over reconciled published output; no guessed source values."""
import hashlib
import json
from pathlib import Path
from .audit import reconcile
from .inputs import InputError, MAX_INTEGER_DIGITS, read_events
from .outputs import check_outputs, OutputError

REASONS = {
    'invalid_utf8': ('record', 'Source bytes are not valid UTF-8.'),
    'invalid_json': ('record', 'JSON is malformed, ambiguous, non-finite, or too deeply nested.'),
    'invalid_unicode': ('record', 'JSON contains an unpaired Unicode surrogate.'),
    'integer_too_large': ('record', f'A JSON integer exceeds the supported {MAX_INTEGER_DIGITS} decimal digits.'),
    'not_object': ('record', 'An event must be a JSON object.'),
    'invalid_event_id': ('event_id', 'Expected a nonempty text event identifier.'),
    'invalid_ingest_seq': ('ingest_seq', 'Expected an integer ingestion sequence, not a boolean or decimal.'),
    'unknown_account': ('account_id', 'Account is missing or absent from the reference configuration.'),
    'unknown_metric': ('metric', 'Metric is missing or absent from period.metrics.'),
    'invalid_units': ('units', 'Expected an integer unit count, not a boolean, decimal or text.'),
    'nonpositive_units': ('units', 'Units must be strictly positive.'),
    'invalid_ts': ('ts', 'Expected a valid ISO timestamp with seconds and explicit UTC offset.'),
    'invalid_ingested_at': ('ingested_at', 'Expected a valid ISO timestamp with seconds and explicit UTC offset.'),
}


def load_verified(out):
    out = Path(out)
    manifest = check_outputs(out)
    data = {'manifest': manifest}
    for key in ('invoices', 'quarantine', 'audit'):
        try:
            raw = (out / f'{key}.json').read_bytes()
            expected = manifest['outputs'][f'{key}.json']
            if len(raw) != expected['bytes'] or hashlib.sha256(raw).hexdigest() != expected['sha256']:
                raise OutputError('output changed during read; do not race a publisher')
            data[key] = json.loads(raw.decode('utf-8'))
        except (OSError, ValueError, RecursionError) as exc:
            raise OutputError(f'{key}.json: unreadable or invalid JSON') from exc
    reconcile(data['invoices'], data['quarantine'], data['audit'], manifest)
    if (out / '.ledger-staging').exists():
        raise OutputError('publication started during read')
    return data


def explain(data, *, event_id=None, line=None, events=None):
    if line is not None and (type(line) is not int or line < 1):
        raise InputError('--line must be a positive physical source line number')
    source_rows = None
    if events is not None:
        raw = Path(events).read_bytes()
        expected = data['manifest']['inputs']['events.jsonl']['sha256']
        if hashlib.sha256(raw).hexdigest() != expected:
            raise InputError('--events differs from the input hash recorded in this run')
        source_rows = {r.line_number: r for r in read_events(raw)}
    decisions = data['audit']['decisions']
    if event_id is not None:
        selected = [d for d in decisions if d['event_id'] == event_id]
    elif line is not None:
        selected = [d for d in decisions if d['line'] == line]
    else:
        selected = [d for d in decisions if d['status'] == 'quarantined']
    if (event_id is not None or line is not None) and not selected:
        raise InputError('no matching record; use the exact event id or physical source line')
    records = []
    for d in selected:
        reasons = []
        for code in d.get('reasons', []):
            field, message = REASONS.get(code, ('record', 'Reason recorded by the engine; no additional interpretation available.'))
            item = {'code': code, 'field': field, 'message': message}
            if source_rows is not None:
                value = source_rows[d['line']].value
                if value is not None and field in value:
                    item['value'] = value[field]
            reasons.append(item)
        record = {'event_id': d['event_id'], 'status': d['status'], 'reasons': reasons,
                  'source': {'file': 'events.jsonl', 'line': d['line'], 'sha256': d['sha256']}}
        if 'canonical_line' in d:
            record['canonical_line'] = d['canonical_line']
        records.append(record)
    return {'records': records, 'count': len(records), 'source_values_verified': source_rows is not None,
            'note': 'Published-output consistency is verified; source field values require matching --events.'}
