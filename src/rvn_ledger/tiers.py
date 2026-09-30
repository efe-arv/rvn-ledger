"""Cumulative usage tiers priced on one plan and currency (source rules 6 and 7).

A tariff is the list `plans[plan_id].prices[currency].metrics[metric]` of
`{from_units, to_units, unit_price_micros}` brackets. Brackets are `[from, to)`
over the account's whole-period units, in the order the tariff states them; they
must start at 0, be contiguous, and end with exactly one open bracket
(`to_units: null`). Anything else is unusable configuration (InputError), never a
silently re-sorted or patched tariff.

Each bracket that carries units is its own invoice line worth
`round_half_up(units_in_bracket * unit_price_micros / 10000)` minor units
(10000 micros of a major unit = 1 minor unit). A bracket with no units produces
no line: an account with no usage gets a subscription-only invoice (rule 9).
"""
from dataclasses import dataclass
from .inputs import InputError
from .money import round_half_up

MICROS_PER_MINOR = 10000


@dataclass(frozen=True)
class Tier:
    from_units: int
    to_units: int | None       # None = open bracket, must be the last one
    unit_price_micros: int


@dataclass(frozen=True)
class UsageLine:
    metric: str
    tier_from: int
    tier_to: int | None
    units: int                 # units that fell into this bracket, always > 0
    unit_price_micros: int
    amount_minor: int


def _count(value, what: str) -> int:
    if type(value) is not int or value < 0:
        raise InputError(f'{what} must be a nonnegative integer')
    return value


def metric_tiers(plans, plan_id: str, currency: str, metric: str) -> tuple[Tier, ...]:
    """Validate and return one metric's tariff on one plan in one currency."""
    if not isinstance(plans, dict):
        raise InputError('plans must be an object keyed by plan_id')
    plan = plans.get(plan_id)
    if not isinstance(plan, dict) or plan.get('plan_id') != plan_id:
        raise InputError(f'unknown plan {plan_id!r}')
    prices = plan.get('prices')
    if not isinstance(prices, dict) or not isinstance(prices.get(currency), dict):
        raise InputError(f'plan {plan_id!r} has no price in {currency}')
    where = f'plan {plan_id!r} {currency} {metric}'
    metrics = prices[currency].get('metrics')
    if not isinstance(metrics, dict) or not isinstance(metrics.get(metric), list) or not metrics[metric]:
        raise InputError(f'{where}: no tariff (nonempty list of tiers) is defined')
    tiers = []
    for entry in metrics[metric]:
        if not isinstance(entry, dict):
            raise InputError(f'{where}: every tier must be an object')
        lower = _count(entry.get('from_units'), f'{where}: from_units')
        upper = entry.get('to_units')
        if upper is not None:
            upper = _count(upper, f'{where}: to_units')
        tiers.append(Tier(lower, upper, _count(entry.get('unit_price_micros'), f'{where}: unit_price_micros')))
    expected_start = 0
    for index, tier in enumerate(tiers):
        if tier.from_units != expected_start:
            raise InputError(f'{where}: tiers must be contiguous from 0 (tier {index} starts at {tier.from_units}, expected {expected_start})')
        if index == len(tiers) - 1:
            if tier.to_units is not None:
                raise InputError(f'{where}: the last tier must be open (to_units null)')
        else:
            if tier.to_units is None:
                raise InputError(f'{where}: only the last tier may be open')
            if tier.to_units <= tier.from_units:
                raise InputError(f'{where}: tier {index} is an empty bracket')
        expected_start = tier.to_units
    return tuple(tiers)


def price_usage(units: int, tiers: tuple[Tier, ...], metric: str) -> list[UsageLine]:
    """Split whole-period units over cumulative brackets; one rounded line per bracket that carries units."""
    if type(units) is not int or units < 0:
        raise ValueError('units must be a nonnegative integer')
    if not tiers:
        raise ValueError('a tariff needs at least one tier')
    lines = []
    for tier in tiers:
        if units <= tier.from_units:
            break
        upper = units if tier.to_units is None else min(units, tier.to_units)
        take = upper - tier.from_units
        if take > 0:
            lines.append(UsageLine(metric, tier.from_units, tier.to_units, take, tier.unit_price_micros,
                                   round_half_up(take * tier.unit_price_micros, MICROS_PER_MINOR)))
    return lines
