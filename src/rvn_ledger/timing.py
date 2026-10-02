"""Account-local half-open periods, with elapsed-time late-arrival limits."""
import hashlib
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from zoneinfo import TZPATH, ZoneInfo, ZoneInfoNotFoundError, available_timezones
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


@lru_cache(maxsize=1)
def iana_zone_keys() -> frozenset:
    return frozenset(available_timezones()) - HOST_DEPENDENT_ZONE_KEYS


@dataclass(frozen=True)
class PeriodBounds:
    start: datetime
    end: datetime
    cutoff: datetime


def period_bounds(start_local: str, end_local: str, zone: str, late_hours: int) -> PeriodBounds:
    """Resolve local midnight to UTC before adding elapsed late-arrival hours."""
    start_day = local_date(start_local, 'period_start_local')   # shared strict parser (review F3)
    end_day = local_date(end_local, 'period_end_local_exclusive')
    try:
        if type(late_hours) is not int or late_hours < 0:
            raise ValueError('late hours must be a nonnegative integer')
        if end_day <= start_day:
            raise ValueError('period end must follow start')
        if not isinstance(zone, str) or zone not in iana_zone_keys():
            raise ValueError('timezone must be an IANA zone key')
        tz = ZoneInfo(zone)
        start = datetime.combine(start_day, time.min, tz).astimezone(timezone.utc)
        end = datetime.combine(end_day, time.min, tz).astimezone(timezone.utc)
        return PeriodBounds(start, end, end + timedelta(hours=late_hours))
    except (ValueError, TypeError, OverflowError, ZoneInfoNotFoundError, OSError) as exc:
        # OSError: a key naming a directory of the tzdata tree (e.g. 'America') raises
        # IsADirectoryError rather than ZoneInfoNotFoundError on this interpreter.
        raise InputError('invalid local period, timezone, or late cutoff') from exc


def _system_version(root: Path) -> str:
    index = root / 'tzdata.zi'
    first = index.read_text(errors='replace').splitlines()[:1] if index.is_file() else []
    return first[0].lstrip('# ').strip().removeprefix('version ').strip() if first else '(version file absent)'


def _package_zone(key: str) -> bytes:
    # Mirrors zoneinfo's package fallback: each directory of the key is a subpackage of tzdata.zoneinfo.
    from importlib import resources
    package, _, resource = ('tzdata.zoneinfo.' + key.replace('/', '.')).rpartition('.')
    return resources.files(package).joinpath(resource).read_bytes()


def zone_provenance(key: str) -> dict:
    """The source kind, data version and SHA-256 of the exact TZif bytes zoneinfo resolves `key` against.

    The record never carries a host path, so it is reproducible across machines holding the same
    data; two runs whose records differ were billed on different zone rules even if the package
    version looks the same. A key found nowhere is recorded as unknown rather than guessed, and
    only a usable IANA key (the set `period_bounds` accepts) is ever read: no path-shaped or
    host-dependent key reaches the file system.
    """
    if not isinstance(key, str) or key not in iana_zone_keys():
        return {'source': 'unknown', 'version': '', 'sha256': None}
    for root in TZPATH:
        path = Path(root) / key
        if path.is_file():
            return {'source': 'system', 'version': _system_version(Path(root)), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    try:
        data = _package_zone(key)
    except Exception:  # noqa: BLE001 - no TZPATH file and no package resource: record that honestly
        return {'source': 'unknown', 'version': '', 'sha256': None}
    try:
        from importlib.metadata import version
        package_version = version('tzdata')
    except Exception:  # noqa: BLE001 - resources present without distribution metadata
        package_version = '(version metadata absent)'
    return {'source': 'tzdata package', 'version': package_version, 'sha256': hashlib.sha256(data).hexdigest()}


def timezone_provenance(zones) -> dict:
    """One provenance record per distinct zone key, in sorted key order (manifest `versions.timezones`)."""
    return {key: zone_provenance(key) for key in sorted(set(zones))}


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
    """
    if not isinstance(accounts, list) or not isinstance(period, dict):
        raise InputError('accounts must be a list and period an object')
    missing = [key for key in PERIOD_KEYS if key not in period]
    if missing:
        raise InputError(f'period is missing {", ".join(missing)}')
    bounds = {}
    for account in accounts:
        if not isinstance(account, dict):
            raise InputError('every account entry must be an object')
        account_id = account.get('account_id')
        if not isinstance(account_id, str) or not account_id:
            raise InputError('account_id must be a nonempty string')
        if account_id in bounds:
            raise InputError(f'duplicate account_id {account_id!r}')
        try:
            bounds[account_id] = period_bounds(period['period_start_local'], period['period_end_local_exclusive'],
                                               account.get('timezone'), period['late_cutoff_hours_after_period_end'])
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
