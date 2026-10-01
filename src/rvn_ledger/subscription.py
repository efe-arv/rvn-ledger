"""Subscription plan segmentation and period-end plan selection (architecture section 4).

Each account's `plan_segments` history is clipped to the local billing period,
counted in whole local calendar days (never elapsed hours / 24, so DST cannot
move a day), and priced with `money.subscription_amount` in the account's own
currency. The plan in effect at period end is the segment covering the final
local day of the period; the exclusive period end itself belongs to the next
period, so a plan starting exactly on the end date is not selected.

Configuration policy (fail closed, source rules 5, 6 and 9):
- A segment covering no local day of the period produces no line.
- Overlapping segments, unknown plans, a missing fee for the account's currency,
  or `days_in_period` disagreeing with the local dates are unusable
  configuration and stop the run with InputError before any invoice exists.
- Uncovered days inside the period carry no plan and no charge (documented
  assumption, not a source rule). The final local day must be covered because
  usage is priced on the plan in effect at period end.
No usage, tiers or invoice assembly happen here.
"""
from dataclasses import dataclass
from datetime import date, timedelta
import re
from .inputs import InputError, local_date
from .money import subscription_amount

# Any ISO 4217-shaped code is accepted; whether it is billable is decided by plans.json,
# which must price every plan the account uses in that currency (rule 9: no exchange rates).
_CURRENCY_CODE = re.compile(r'[A-Z]{3}', re.ASCII)


@dataclass(frozen=True)
class SubscriptionSegment:
    plan_id: str
    start: date        # clipped to the period, inclusive
    end: date          # clipped to the period, exclusive
    days: int
    fee_minor: int     # full-period fee in the account currency
    amount_minor: int  # round_half_up(fee_minor * days / days_in_period)


@dataclass(frozen=True)
class Subscription:
    account_id: str
    currency: str
    segments: tuple[SubscriptionSegment, ...]   # chronological, only segments with days > 0
    period_end_plan_id: str                     # plan covering the final local day of the period


def _local_date(value, what: str) -> date:
    return local_date(value, what)  # the shared strict parser also used by timing (review F3)


def period_days(period: dict) -> tuple[date, date, int]:
    """Local period dates plus the proration denominator; the two must agree."""
    if not isinstance(period, dict):
        raise InputError('period must be an object')
    for key in ('period_start_local', 'period_end_local_exclusive', 'days_in_period'):
        if key not in period:
            raise InputError(f'period is missing {key}')
    start = _local_date(period['period_start_local'], 'period_start_local')
    end = _local_date(period['period_end_local_exclusive'], 'period_end_local_exclusive')
    if end <= start:
        raise InputError('period end must follow start')
    days = period['days_in_period']
    if type(days) is not int or days <= 0:
        raise InputError('days_in_period must be a positive integer')
    if days != (end - start).days:
        raise InputError(f'days_in_period {days} disagrees with the local period of {(end - start).days} days')
    return start, end, days


def _fee(plans, plan_id: str, currency: str) -> int:
    if not isinstance(plans, dict):
        raise InputError('plans must be an object keyed by plan_id')
    plan = plans.get(plan_id)
    if not isinstance(plan, dict) or plan.get('plan_id') != plan_id:
        raise InputError(f'unknown plan {plan_id!r}')
    prices = plan.get('prices')
    if not isinstance(prices, dict) or not isinstance(prices.get(currency), dict):
        raise InputError(f'plan {plan_id!r} has no price in {currency}')
    fee = prices[currency].get('subscription_fee_minor')
    if type(fee) is not int or fee < 0:
        raise InputError(f'plan {plan_id!r} {currency} subscription_fee_minor must be a nonnegative integer')
    return fee


def _segments(account_id: str, raw) -> list[tuple[date, date, str]]:
    if not isinstance(raw, list):
        raise InputError(f'{account_id}: plan_segments must be a list')
    segments = []
    for entry in raw:
        if not isinstance(entry, dict) or any(key not in entry for key in ('plan_id', 'from', 'to')):
            raise InputError(f'{account_id}: every plan segment needs plan_id, from and to')
        plan_id = entry['plan_id']
        if not isinstance(plan_id, str) or not plan_id:
            raise InputError(f'{account_id}: plan_id must be a nonempty string')
        start = _local_date(entry['from'], f'{account_id}: segment from')
        end = _local_date(entry['to'], f'{account_id}: segment to')
        if end <= start:
            raise InputError(f'{account_id}: segment to must follow from')
        segments.append((start, end, plan_id))
    segments.sort()
    for (_, previous_end, _), (next_start, _, _) in zip(segments, segments[1:]):
        if next_start < previous_end:
            raise InputError(f'{account_id}: plan segments overlap')
    return segments


def account_subscription(account: dict, period: dict, plans: dict) -> Subscription:
    """Clip one account's plan history to the period and price each covered segment."""
    if not isinstance(account, dict):
        raise InputError('every account entry must be an object')
    account_id = account.get('account_id')
    if not isinstance(account_id, str) or not account_id:
        raise InputError('account_id must be a nonempty string')
    currency = account.get('currency')
    if not isinstance(currency, str) or not _CURRENCY_CODE.fullmatch(currency):
        raise InputError(f'{account_id}: currency must be a three-letter code such as USD')
    period_start, period_end, denominator = period_days(period)
    last_day = period_end - timedelta(days=1)
    segments, end_plan = [], None
    for start, end, plan_id in _segments(account_id, account.get('plan_segments')):
        fee = _fee(plans, plan_id, currency)  # every referenced plan must be priceable, even outside the period
        clipped_start, clipped_end = max(start, period_start), min(end, period_end)
        days = (clipped_end - clipped_start).days
        if days <= 0:
            continue
        segments.append(SubscriptionSegment(plan_id, clipped_start, clipped_end, days, fee,
                                            subscription_amount(fee, days, denominator)))
        if clipped_start <= last_day < clipped_end:
            end_plan = plan_id
    if end_plan is None:
        raise InputError(f'{account_id}: no plan segment covers the final local day {last_day.isoformat()}')
    return Subscription(account_id, currency, tuple(segments), end_plan)


def all_subscriptions(accounts: list, period: dict, plans: dict) -> dict[str, Subscription]:
    """Resolve every account eagerly, sorted by account_id; any unusable entry stops the run."""
    if not isinstance(accounts, list):
        raise InputError('accounts must be a list')
    result = {}
    for account in accounts:
        subscription = account_subscription(account, period, plans)
        if subscription.account_id in result:
            raise InputError(f'duplicate account_id {subscription.account_id!r}')
        result[subscription.account_id] = subscription
    return dict(sorted(result.items()))
