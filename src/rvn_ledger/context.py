"""Validated billing configuration shared by all stages of one run."""
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from .inputs import InputError, period_metrics
from .subscription import Subscription, all_subscriptions
from .tiers import Tier, metric_tiers
from .timing import PeriodBounds, account_bounds


@dataclass(frozen=True)
class BillingAccount:
    """Resolved subscription, credit and tariffs for one account."""
    account_id: str
    timezone: str
    credit_minor: int
    subscription: Subscription
    tariffs: Mapping[str, tuple[Tier, ...]]


@dataclass(frozen=True)
class BillingContext:
    """Read-only, eagerly validated configuration for one billing period."""
    metrics: tuple[str, ...]
    accounts: Mapping[str, BillingAccount]
    bounds: Mapping[str, PeriodBounds]


def account_credit(account: dict) -> int:
    """Read an exact nonnegative credit; missing credit is not implicitly zero."""
    if 'credit_minor' not in account:
        raise InputError(f"{account.get('account_id')}: credit_minor is missing")
    credit = account['credit_minor']
    if type(credit) is not int or credit < 0:
        raise InputError(f"{account.get('account_id')}: credit_minor must be a nonnegative integer")
    return credit


def prepare_context(accounts: list, period: dict, plans: dict) -> BillingContext:
    """Resolve required configuration once, including accounts with no usage."""
    if not isinstance(accounts, list) or not accounts:
        raise InputError('accounts must be a nonempty list')
    bounds = account_bounds(accounts, period)
    metrics = period_metrics(period)
    subscriptions = all_subscriptions(accounts, period, plans)
    by_id = {account['account_id']: account for account in accounts}
    prepared = {}
    for account_id, subscription in subscriptions.items():
        account = by_id[account_id]
        credit = account_credit(account)
        tariffs = {metric: metric_tiers(plans, subscription.period_end_plan_id, subscription.currency, metric)
                   for metric in metrics}
        prepared[account_id] = BillingAccount(account_id, account['timezone'], credit,
                                              subscription, MappingProxyType(tariffs))
    return BillingContext(metrics, MappingProxyType(prepared), MappingProxyType(bounds))
