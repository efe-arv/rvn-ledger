"""Invoice assembly: subscription segments + usage tiers, per-line rounding, then credit (rules 5-9).

One invoice per account, sorted by account_id, in the account's own currency.
Lines are the chronological subscription segments followed by the usage tiers
per metric (in period.metrics order); the subtotal is the sum of the already
rounded lines; the credit is capped at the subtotal; the total is never negative.

`quarantined_count` counts the quarantined raw lines whose parsed `account_id`
names this account (documented assumption A7): a record for an unknown account
or without a readable payload belongs to no invoice, so it is only in
quarantine.json. Excluded (out-of-period, late) and duplicate records are not
quarantined and are not counted here.

Configuration is resolved eagerly and fails closed: every account's period-end
plan must price every metric of the period in the account's currency even when
usage is zero, and `credit_minor` must be a nonnegative integer.
"""
from dataclasses import dataclass
from .aggregation import aggregate_usage
from .context import BillingAccount, BillingContext, prepare_context
from .context import account_credit as account_credit  # compatibility for verifier callers
from .inputs import EventRow
from .money import apply_credit
from .selection import Classification
from .subscription import Subscription
from .tiers import UsageLine, price_usage


@dataclass(frozen=True)
class Invoice:
    account_id: str
    currency: str
    timezone: str
    billable_units: tuple[tuple[str, int], ...]     # (metric, accepted units) in period.metrics order
    subscription: Subscription
    usage_lines: tuple[UsageLine, ...]
    subtotal_minor: int
    credit_minor: int
    credit_applied_minor: int
    credit_remaining_minor: int
    total_minor: int
    quarantined_lines: tuple[int, ...]              # raw line numbers attributed to this account

    @property
    def quarantined_count(self) -> int:
        return len(self.quarantined_lines)

    def as_contract(self) -> dict:
        """Exactly the period.json output contract, in its key order; nothing extra."""
        lines = [{'kind': 'subscription', 'plan_id': s.plan_id, 'days': s.days, 'amount_minor': s.amount_minor}
                 for s in self.subscription.segments]
        lines += [{'kind': 'usage', 'metric': u.metric, 'tier_from': u.tier_from, 'tier_to': u.tier_to, 'units': u.units,
                   'amount_minor': u.amount_minor} for u in self.usage_lines]
        return {'account_id': self.account_id, 'currency': self.currency, 'timezone': self.timezone,
                'billable_units': dict(self.billable_units), 'lines': lines, 'subtotal_minor': self.subtotal_minor,
                'credit_applied_minor': self.credit_applied_minor, 'credit_remaining_minor': self.credit_remaining_minor,
                'total_minor': self.total_minor, 'quarantined_count': self.quarantined_count}


def quarantined_by_account(rows: list[EventRow], classification: Classification, account_ids: set[str]) -> dict[str, list[int]]:
    """Quarantined raw lines whose payload names a known account, in source order."""
    result = {account_id: [] for account_id in account_ids}
    for row in rows:
        if classification.statuses.get(row.line_number) != 'quarantined' or row.value is None:
            continue
        account = row.value.get('account_id')
        if isinstance(account, str) and account in result:
            result[account].append(row.line_number)
    return result


def build_invoice(account: BillingAccount, metrics: tuple[str, ...], usage: dict,
                  quarantined_lines: list[int]) -> Invoice:
    """One account: subscription segments, usage on the period-end plan, credit."""
    subscription = account.subscription
    usage_lines = []
    billable = []
    for metric in metrics:
        units = usage[metric]['units']
        billable.append((metric, units))
        usage_lines.extend(price_usage(units, account.tariffs[metric], metric))
    amounts = [s.amount_minor for s in subscription.segments] + [u.amount_minor for u in usage_lines]
    subtotal, applied, remaining, total = apply_credit(amounts, account.credit_minor)
    return Invoice(subscription.account_id, subscription.currency, account.timezone, tuple(billable), subscription,
                   tuple(usage_lines), subtotal, account.credit_minor, applied, remaining, total, tuple(quarantined_lines))


def build_invoices(accounts: list, period: dict, plans: dict, rows: list[EventRow], classification: Classification) -> list[Invoice]:
    """Every account, sorted by account_id; any unusable configuration stops the run before an invoice exists."""
    context = prepare_context(accounts, period, plans)
    usage = aggregate_usage(classification.accepted, context.accounts, context.metrics)
    return assemble_invoices(context, rows, classification, usage)


def assemble_invoices(context: BillingContext, rows: list[EventRow], classification: Classification, usage: dict) -> list[Invoice]:
    """Assemble invoices from the same prepared configuration and totals as the audit."""
    quarantined = quarantined_by_account(rows, classification, set(context.accounts))
    return [build_invoice(account, context.metrics, usage[account_id], quarantined[account_id])
            for account_id, account in context.accounts.items()]
