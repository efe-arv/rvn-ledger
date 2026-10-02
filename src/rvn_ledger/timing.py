"""Account-local half-open periods, with elapsed-time late-arrival limits."""
import hashlib
import struct
import zoneinfo
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo, available_timezones
from .inputs import InputError, local_date
from .validation import ValidatedEvent

PERIOD_KEYS = ('period_start_local', 'period_end_local_exclusive', 'late_cutoff_hours_after_period_end')
# Keys the tzdata tree may resolve but that do not name a real place: 'localtime' is the
# host's own zone, 'posixrules' and 'Factory' are build artefacts. Billing on them would
# depend on the server, so they are unusable configuration even where ZoneInfo accepts them.
HOST_DEPENDENT_ZONE_KEYS = frozenset({'localtime', 'posixrules', 'Factory'})
# Where a zone's TZif bytes can come from, in zoneinfo's own lookup order: a file under one of the
# TZPATH roots shadows the tzdata package even when it is the only file in that root (review F3).
ZONE_SOURCES = ('system', 'tzdata package', 'unknown')


@lru_cache(maxsize=4)
def _iana_zone_keys(tzpath: tuple) -> frozenset:
    return frozenset(available_timezones()) - HOST_DEPENDENT_ZONE_KEYS


def iana_zone_keys() -> frozenset:
    """Usable zone keys for the search path in force now; a `reset_tzpath` gets a fresh listing."""
    return _iana_zone_keys(zoneinfo.TZPATH)


@dataclass(frozen=True)
class ResolvedZone:
    """One zone's rules together with the provenance of the exact bytes they were built from.

    `tz` is constructed with `ZoneInfo.from_file` from the bytes `sha256` names, never taken from
    the process-wide `ZoneInfo(key)` cache: that cache keeps whatever bytes the first lookup in the
    process found and never notices a later `reset_tzpath` or a changed file, so a record taken by
    re-reading the file could describe rules the run did not bill on.
    """
    key: str
    source: str
    version: str
    sha256: str
    tz: ZoneInfo

    @property
    def record(self) -> dict:
        return {'source': self.source, 'version': self.version, 'sha256': self.sha256}


@dataclass(frozen=True)
class PeriodBounds:
    start: datetime
    end: datetime
    cutoff: datetime
    zone: ResolvedZone


def _system_version(root: Path) -> str:
    index = root / 'tzdata.zi'
    first = index.read_text(errors='replace').splitlines()[:1] if index.is_file() else []
    return first[0].lstrip('# ').strip().removeprefix('version ').strip() if first else '(version file absent)'


def _package_zone(key: str) -> bytes:
    # Mirrors zoneinfo's package fallback: each directory of the key is a subpackage of tzdata.zoneinfo.
    from importlib import resources
    package, _, resource = ('tzdata.zoneinfo.' + key.replace('/', '.')).rpartition('.')
    return resources.files(package).joinpath(resource).read_bytes()


def _package_version() -> str:
    try:
        from importlib.metadata import version
        return version('tzdata')
    except Exception:  # noqa: BLE001 - resources present without distribution metadata
        return '(version metadata absent)'


def _zone_bytes(key: str) -> tuple[str, str, bytes]:
    """Source kind, data version and TZif bytes for `key`, in zoneinfo's lookup order, read once now.

    Only a usable IANA key (the set `period_bounds` accepts) is ever read: no path-shaped or
    host-dependent key reaches the file system. The search path is the live `zoneinfo.TZPATH`.
    """
    if not isinstance(key, str) or key not in iana_zone_keys():
        raise InputError('timezone must be an IANA zone key')
    for root in zoneinfo.TZPATH:
        path = Path(root) / key
        if path.is_file():
            try:
                return 'system', _system_version(Path(root)), path.read_bytes()
            except OSError as exc:
                raise InputError(f'timezone {key!r}: cannot read its TZif file') from exc
    try:
        return 'tzdata package', _package_version(), _package_zone(key)
    except Exception as exc:  # noqa: BLE001 - no TZPATH file and no package resource
        raise InputError(f'timezone {key!r} has no TZif data on this host') from exc


def resolve_zone(key: str) -> ResolvedZone:
    """Build `key`'s rules from exactly the TZif bytes that are recorded for it."""
    source, version, data = _zone_bytes(key)
    try:
        tz = ZoneInfo.from_file(BytesIO(data), key=key)
    except (ValueError, OSError, EOFError, struct.error) as exc:
        raise InputError(f'timezone {key!r}: its TZif data is not a valid time zone file') from exc
    return ResolvedZone(key, source, version, hashlib.sha256(data).hexdigest(), tz)


def period_bounds(start_local: str, end_local: str, zone, late_hours: int) -> PeriodBounds:
    """Resolve local midnight to UTC before adding elapsed late-arrival hours.

    `zone` is an IANA key, resolved here, or a `ResolvedZone` shared by every account in that zone.
    """
    start_day = local_date(start_local, 'period_start_local')   # shared strict parser (review F3)
    end_day = local_date(end_local, 'period_end_local_exclusive')
    try:
        if type(late_hours) is not int or late_hours < 0:
            raise ValueError('late hours must be a nonnegative integer')
        if end_day <= start_day:
            raise ValueError('period end must follow start')
        resolved = zone if isinstance(zone, ResolvedZone) else resolve_zone(zone)
        start = datetime.combine(start_day, time.min, resolved.tz).astimezone(timezone.utc)
        end = datetime.combine(end_day, time.min, resolved.tz).astimezone(timezone.utc)
        return PeriodBounds(start, end, end + timedelta(hours=late_hours), resolved)
    except (ValueError, TypeError, OverflowError) as exc:
        raise InputError('invalid local period, timezone, or late cutoff') from exc


def zone_provenance(key: str) -> dict:
    """The provenance record of the TZif bytes `key` resolves against at this moment.

    This is a fresh read: it describes the host now, not what a run was billed on. The run's own
    records come from the `ResolvedZone` objects in its `PeriodBounds`. A key found nowhere is
    recorded as unknown rather than guessed. The record never carries a host path, so it is
    reproducible across machines holding the same data.
    """
    try:
        return resolve_zone(key).record
    except InputError:
        return {'source': 'unknown', 'version': '', 'sha256': None}


def timezone_provenance(zones) -> dict:
    """One provenance record per distinct zone key, in sorted key order (manifest `versions.timezones`).

    `ResolvedZone` entries contribute the record of the bytes their rules were built from; a bare
    key is read now (see `zone_provenance`). Two resolutions of one key with different bytes in one
    run would make the record a lie, so that is a defect, not a merge.
    """
    records = {}
    for zone in zones:
        key, record = (zone.key, zone.record) if isinstance(zone, ResolvedZone) else (zone, zone_provenance(zone))
        if records.setdefault(key, record) != record:
            raise RuntimeError(f'time zone {key!r} was resolved against two different TZif byte sequences in one run')
    return {key: records[key] for key in sorted(records)}


def provenance_summary(provenance: dict) -> str:
    """One line for `versions.tzdata`: the single database every zone came from, or an explicit mix."""
    summaries = sorted({f"{record['source']} {record['version']}".strip() for record in provenance.values()})
    if not summaries:
        return 'unknown'
    return summaries[0] if len(summaries) == 1 else 'mixed: ' + ', '.join(summaries)


def account_bounds(accounts: list, period: dict) -> dict[str, PeriodBounds]:
    """Resolve every account's local period up front; any unusable entry stops the run.

    This is deliberately eager: an account with a bad timezone fails the run even
    when no event references it, instead of surfacing only if an event arrives.
    Each distinct zone key is read once; every account in that zone shares the
    resolution, so one run bills one zone on one set of bytes and records those.
    """
    if not isinstance(accounts, list) or not isinstance(period, dict):
        raise InputError('accounts must be a list and period an object')
    missing = [key for key in PERIOD_KEYS if key not in period]
    if missing:
        raise InputError(f'period is missing {", ".join(missing)}')
    bounds = {}
    resolved = {}
    for account in accounts:
        if not isinstance(account, dict):
            raise InputError('every account entry must be an object')
        account_id = account.get('account_id')
        if not isinstance(account_id, str) or not account_id:
            raise InputError('account_id must be a nonempty string')
        if account_id in bounds:
            raise InputError(f'duplicate account_id {account_id!r}')
        try:
            zone = account.get('timezone')
            if not isinstance(zone, str):
                raise InputError('timezone must be an IANA zone key')
            if zone not in resolved:
                resolved[zone] = resolve_zone(zone)
            bounds[account_id] = period_bounds(period['period_start_local'], period['period_end_local_exclusive'],
                                               resolved[zone], period['late_cutoff_hours_after_period_end'])
        except InputError as exc:
            raise InputError(f'{account_id}: {exc}') from exc
    return bounds


def classify_time(event: ValidatedEvent, bounds: PeriodBounds) -> str:
    """Only valid deduplication winners enter; period exclusion precedes lateness."""
    if event.reasons or event.ts is None or event.ingested_at is None:
        raise ValueError('time filtering requires a valid event')
    # Compare exact instants, preserving fractions smaller than a microsecond.
    from .validation import utc_seconds
    ts = event.ts_exact if event.ts_exact is not None else utc_seconds(event.ts)
    ingestion = event.ingested_at_exact if event.ingested_at_exact is not None else utc_seconds(event.ingested_at)
    if not utc_seconds(bounds.start) <= ts < utc_seconds(bounds.end):
        return 'excluded_out_of_period'
    if ingestion > utc_seconds(bounds.cutoff):
        return 'excluded_late'
    return 'accepted'
