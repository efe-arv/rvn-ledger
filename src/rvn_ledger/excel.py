"""Bounded XLSX reports and lossless, explicitly versioned input workbooks.

Input workbooks preserve original JSON text/bytes, not Excel's inferred numeric
or date types. Report workbooks are intentionally NOT importable billing input.
"""
import base64
import io
import json
import os
from pathlib import Path
import re
import zipfile
import zlib
from xml.etree.ElementTree import ParseError
from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring as safe_xml
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill
from .inputs import InputError
from .outputs import OutputError
from .pipeline import INPUT_NAMES

MAX_ARCHIVE = 20 * 1024 * 1024
MAX_EXPANDED = 64 * 1024 * 1024
MAX_ROWS = 100000
MAX_CELL = 30000
SCHEMA = 'rvn-ledger-inputs-v1'
_ILLEGAL = re.compile(r'[\x00-\x08\x0b-\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]')


def _text(value):
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=True, separators=(',', ':'))
    if isinstance(value, str):
        # JSON escape invalid XML characters rather than allowing openpyxl to strip them.
        if _ILLEGAL.search(value):
            value = json.dumps(value, ensure_ascii=True)
        if len(value.encode('utf-16-le')) // 2 > MAX_CELL:
            raise InputError('a report cell exceeds the supported Excel text limit')
    if type(value) is int and abs(value) >= 10**15:
        return str(value)  # Excel silently rounds beyond 15 decimal digits.
    return value


def _sheet(wb, name, headers, rows):
    ws = wb.create_sheet(name)
    for row in [headers, *rows]:
        if ws.max_row >= MAX_ROWS:
            raise InputError(f'{name}: too many rows (limit {MAX_ROWS})')
        values = [_text(v) for v in row]
        ws.append(values)
        for cell, value in zip(ws[ws.max_row], values):
            if isinstance(value, str):
                cell.data_type = 's'  # No Excel formula execution, including leading '='.
                cell.number_format = '@'
    ws.freeze_panes = 'A2'
    ws.auto_filter.ref = ws.dimensions
    for cell in ws[1]:
        cell.font = Font(bold=True, color='FFFFFF')
        cell.fill = PatternFill('solid', fgColor='24384D')
    for column in ws.columns:
        ws.column_dimensions[column[0].column_letter].width = min(64, max(14, max(len(str(c.value or '')) for c in column) + 2))
    return ws


def _save(wb, path):
    path = Path(path)
    # Serialize before touching the destination; never overwrite an existing file.
    stream = io.BytesIO()
    try:
        wb.save(stream)
        content = stream.getvalue()
        if len(content) > MAX_ARCHIVE:
            raise InputError('workbook exceeds the supported archive size')
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            if sum(i.file_size for i in archive.infolist()) > MAX_EXPANDED:
                raise InputError('workbook exceeds the supported expanded size')
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            raise OutputError('destination exists; choose a new --file path') from exc
        try:
            with os.fdopen(fd, 'wb') as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            path.unlink(missing_ok=True)
            raise
    finally:
        wb.close()


def export_report(data, path):
    wb = Workbook()
    wb.remove(wb.active)
    manifest = data['manifest']
    _sheet(wb, 'Summary', ['section', 'currency_or_key', 'field', 'value'],
           [['counts', k, 'count', v] for k, v in manifest['counts'].items()] +
           [['totals_by_currency', c, k, v] for c, totals in manifest['totals_by_currency'].items() for k, v in totals.items()] +
           [['note', 'precision', 'policy', 'Amounts are exact minor units; integers >= 15 digits are TEXT. No mixed-currency total.'],
            ['note', 'report', 'policy', 'Read-only report, not an import workbook. Source JSON outputs remain authoritative.']])
    keys = ['account_id', 'currency', 'timezone', 'subtotal_minor', 'credit_applied_minor', 'credit_remaining_minor', 'total_minor', 'quarantined_count']
    _sheet(wb, 'Invoices', keys, [[inv[k] for k in keys] for inv in data['invoices']])
    _sheet(wb, 'InvoiceLines', ['account_id', 'currency', 'line_number', 'details_json'],
           [[inv['account_id'], inv['currency'], i, line] for inv in data['invoices'] for i, line in enumerate(inv['lines'], 1)])
    _sheet(wb, 'Quarantine', ['source_file', 'line', 'event_id', 'primary_reason', 'all_reasons', 'source_sha256'],
           [['events.jsonl', d['line'], d['event_id'], d['reasons'][0], d['reasons'], d['sha256']]
            for d in data['audit']['decisions'] if d['status'] == 'quarantined'])
    _sheet(wb, 'Sources', ['account_id', 'metric', 'event_id', 'line', 'units'],
           [[account, metric, s['event_id'], s['line'], s['units']]
            for account, trail in data['audit']['invoices'].items() for metric, sources in trail['usage_sources'].items() for s in sources])
    _sheet(wb, 'InputHashes', ['input_file', 'sha256', 'bytes'],
           [[name, meta['sha256'], meta['bytes']] for name, meta in manifest['inputs'].items()])
    _save(wb, path)


def _encode(raw):
    try:
        text = raw.decode('utf-8')
        if _ILLEGAL.search(text) or '\r' in text or len(text.encode('utf-16-le')) // 2 > MAX_CELL:
            raise ValueError('not representable in one Excel cell')
        return 'utf8', text
    except (UnicodeDecodeError, ValueError):
        text = base64.b64encode(raw).decode('ascii')
        if len(text) > MAX_CELL:
            raise InputError('source row exceeds workbook cell limit; use JSONL directly')
        return 'base64', text


def export_inputs(raw, path):
    if set(raw) != set(INPUT_NAMES):
        raise InputError('input workbook requires exactly the four input files')
    if sum(len(v) for v in raw.values()) > MAX_ARCHIVE:
        raise InputError('input files exceed workbook limit')
    wb = Workbook()
    wb.remove(wb.active)
    _sheet(wb, 'Schema', ['schema'], [[SCHEMA]])
    event_rows = []
    lines = raw['events.jsonl'].split(b'\n')
    if lines[-1] == b'':
        lines.pop()
    for number, line in enumerate(lines, 1):
        eol = 'LF' if number < len(lines) or raw['events.jsonl'].endswith(b'\n') else 'NONE'
        encoding, text = _encode(line)
        event_rows.append([number, encoding, text, eol])
    _sheet(wb, 'Events', ['line', 'encoding', 'text', 'eol'], event_rows)
    config_rows = []
    for name in INPUT_NAMES[1:]:
        # Chunk bytes, base64 when a UTF-8 sequence splits at the boundary.
        content = raw[name]
        for part, start in enumerate(range(0, max(1, len(content)), 15000), 1):
            encoding, text = _encode(content[start:start + 15000])
            config_rows.append([name, part, encoding, text])
    _sheet(wb, 'Config', ['file', 'part', 'encoding', 'text'], config_rows)
    _save(wb, path)


def _decode(encoding, text):
    # Empty text cells are saved as None by Excel/openpyxl. Nothing else is coerced.
    if text is None:
        text = ''
    if not isinstance(text, str) or len(text.encode('utf-16-le')) // 2 > MAX_CELL:
        raise InputError('input workbook text cells must be text within the size limit')
    if encoding == 'utf8':
        if _ILLEGAL.search(text):
            raise InputError('invalid Unicode in workbook text')
        return text.encode('utf-8')
    if encoding == 'base64':
        try:
            return base64.b64decode(text, validate=True)
        except (ValueError, UnicodeEncodeError) as exc:
            raise InputError('invalid base64 source cell') from exc
    raise InputError('encoding must be utf8 or base64')


def import_inputs(path):
    """Parse the strict transport schema; CLI validates configuration/billing before publication."""
    try:
        path = Path(path)
        if path.stat().st_size > MAX_ARCHIVE:
            raise InputError('workbook archive exceeds size limit')
        content = path.read_bytes()
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            infos = archive.infolist()
            if len(infos) > 1000 or len({i.filename for i in infos}) != len(infos):
                raise InputError('invalid or oversized workbook archive directory')
            if sum(i.file_size for i in infos) > MAX_EXPANDED:
                raise InputError('workbook expanded size exceeds limit')
            for info in infos:
                if info.flag_bits & 1:
                    raise InputError('encrypted workbook archives are not supported')
                if info.flag_bits & ~0x080e:
                    raise InputError('unsupported workbook archive flags')
                if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                    raise InputError('unsupported workbook archive compression')
                if any(x in ('..', '') for x in info.filename.split('/')) or info.filename.startswith('/'):
                    raise InputError('invalid archive member path')
                if any(token in info.filename.lower() for token in ('vbaproject', 'externallinks/', 'embeddings/')):
                    raise InputError('macros, external links and embedded objects are not supported')
                if info.filename.endswith(('.xml', '.rels')):
                    xml = archive.read(info)
                    if b'<!DOCTYPE' in xml.upper() or b'<!ENTITY' in xml.upper():
                        raise InputError('XML entities and document types are not supported')
                    root = safe_xml(xml, forbid_dtd=True, forbid_entities=True, forbid_external=True)
                    if info.filename.endswith('.rels') and any(
                            node.get('TargetMode', '').lower() == 'external' for node in root.iter()):
                        raise InputError('external workbook relationships are not supported')
        wb = load_workbook(io.BytesIO(content), read_only=True, data_only=False, keep_links=False)
        try:
            if wb.sheetnames != ['Schema', 'Events', 'Config']:
                raise InputError('expected the rvn-ledger input workbook; report workbooks cannot be imported')
            tables = {}
            headers = {'Schema': ['schema'], 'Events': ['line', 'encoding', 'text', 'eol'], 'Config': ['file', 'part', 'encoding', 'text']}
            for name in wb.sheetnames:
                ws = wb[name]
                ws.reset_dimensions()  # Do not trust attacker-controlled dimension metadata.
                rows = []
                for n, row in enumerate(ws.iter_rows(), 1):
                    if n > MAX_ROWS or len(row) != len(headers[name]):
                        raise InputError(f'{name}: row limit or column shape mismatch')
                    if any(c.data_type in ('f', 'e') for c in row):
                        raise InputError('formulas and Excel errors are not accepted as input')
                    rows.append([c.value for c in row])
                if not rows or rows[0] != headers[name]:
                    raise InputError(f'{name}: invalid headers')
                tables[name] = rows[1:]
            if tables['Schema'] != [[SCHEMA]]:
                raise InputError('unsupported input workbook schema version')
            events = bytearray()
            rows = tables['Events']
            for i, (line, encoding, text, eol) in enumerate(rows, 1):
                if type(line) is not int or line != i or eol not in ('LF', 'NONE') or (i < len(rows) and eol != 'LF'):
                    raise InputError('event lines must be consecutive and only the final row can omit LF')
                raw = _decode(encoding, text)
                if b'\n' in raw:
                    raise InputError('event row contains embedded LF; put each physical line in its own row')
                if i == len(rows) and eol == 'NONE' and not raw:
                    raise InputError('empty final event without LF is ambiguous; remove the row')
                events.extend(raw + (b'\n' if eol == 'LF' else b''))
            result = {'events.jsonl': bytes(events)}
            parts = {name: [] for name in INPUT_NAMES[1:]}
            for name, part, encoding, text in tables['Config']:
                if name not in parts or type(part) is not int or part != len(parts[name]) + 1:
                    raise InputError('config file names and consecutive part numbers must match the schema')
                parts[name].append(_decode(encoding, text))
            if any(not chunks for chunks in parts.values()):
                raise InputError('one or more configuration files are missing')
            result.update({name: b''.join(chunks) for name, chunks in parts.items()})
            if sum(len(v) for v in result.values()) > MAX_ARCHIVE:
                raise InputError('decoded inputs exceed size limit')
            return result
        finally:
            wb.close()
    except InputError:
        raise
    except (OSError, ValueError, TypeError, KeyError, zipfile.BadZipFile, EOFError, OverflowError,
            ParseError, DefusedXmlException, UnicodeError, IndexError, AttributeError, zlib.error) as exc:
        raise InputError('unreadable or malformed XLSX input workbook') from exc
