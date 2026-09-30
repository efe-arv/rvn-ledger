"""Exact nonnegative minor-unit money operations; no floating point.

Every entry point enforces the contract itself: amounts are Python ints that
are not bools and not negative. Callers do not get to pass floats or booleans
"just this once"; a violation is a ValueError before any arithmetic happens.
"""


def _money(value, name):
    if type(value) is not int or value < 0:
        raise ValueError(f'{name} must be a nonnegative integer')
    return value


def round_half_up(numerator, denominator):
    if type(numerator) is not int or numerator < 0:
        raise ValueError('numerator must be a nonnegative integer')
    if type(denominator) is not int or denominator <= 0:
        raise ValueError('denominator must be a positive integer')
    return (2 * numerator + denominator) // (2 * denominator)


def subscription_amount(fee_minor, days, period_days):
    """round_half_up(fee_minor * days / period_days) for a segment of whole local days."""
    _money(fee_minor, 'fee_minor')
    if type(period_days) is not int or period_days <= 0:
        raise ValueError('period_days must be a positive integer')
    if type(days) is not int or not 0 <= days <= period_days:
        raise ValueError('days must be an integer within 0..period_days')
    return round_half_up(fee_minor * days, period_days)


def apply_credit(line_amounts, credit_minor):
    """Return (subtotal, credit_applied, credit_remaining, total); total is never negative."""
    # Only a list or tuple is a sequence of lines: a dict would sum its keys and a set would
    # collapse equal line amounts (two 1-minor-unit lines would become one).
    if not isinstance(line_amounts, (list, tuple)):
        raise ValueError('line_amounts must be a list or tuple of nonnegative integers')
    lines = [_money(amount, 'line amount') for amount in line_amounts]
    _money(credit_minor, 'credit_minor')
    subtotal = sum(lines)
    applied = min(subtotal, credit_minor)
    return subtotal, applied, credit_minor - applied, subtotal - applied
