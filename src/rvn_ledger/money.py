"""Exact nonnegative minor-unit money rules; no floating point.

This module is the single home of the arithmetic rules (billing rules 5, 7 and 8):
rounding, subscription proration, usage line pricing and credit application.
The invoice builder and the audit reconciliation both call these functions, so a
rule changed here changes everywhere at once. The human-readable formulas
written to audit.json are produced here as well, next to the arithmetic they
describe.

Every entry point enforces the contract itself: amounts are Python ints that
are not bools and not negative. A violation is a ValueError before any
arithmetic happens.
"""

MICROS_PER_MINOR = 10000   # unit prices are in micros of a major unit; 10000 micros = 1 minor unit


def _money(value, name):
    if type(value) is not int or value < 0:
        raise ValueError(f'{name} must be a nonnegative integer')
    return value


def round_half_up(numerator, denominator):
    """Round numerator / denominator to the nearest integer, halves up, using integers only."""
    if type(numerator) is not int or numerator < 0:
        raise ValueError('numerator must be a nonnegative integer')
    if type(denominator) is not int or denominator <= 0:
        raise ValueError('denominator must be a positive integer')
    return (2 * numerator + denominator) // (2 * denominator)


def subscription_amount(fee_minor, days, period_days):
    """Rule 5: round_half_up(fee_minor * days / period_days) for a segment of whole local days."""
    _money(fee_minor, 'fee_minor')
    if type(period_days) is not int or period_days <= 0:
        raise ValueError('period_days must be a positive integer')
    if type(days) is not int or not 0 <= days <= period_days:
        raise ValueError('days must be an integer within 0..period_days')
    return round_half_up(fee_minor * days, period_days)


def usage_amount(units, unit_price_micros):
    """Rule 7: one usage line is round_half_up(units * unit_price_micros / 10000)."""
    _money(units, 'units')
    _money(unit_price_micros, 'unit_price_micros')
    return round_half_up(units * unit_price_micros, MICROS_PER_MINOR)


def subscription_formula(fee_minor, days, period_days, amount_minor):
    return f'round_half_up({fee_minor} * {days} / {period_days}) = {amount_minor}'


def usage_formula(units, unit_price_micros, amount_minor):
    return f'round_half_up({units} * {unit_price_micros} / {MICROS_PER_MINOR}) = {amount_minor}'


def apply_credit(line_amounts, credit_minor):
    """Rule 8: return (subtotal, credit_applied, credit_remaining, total); total is never negative.

    The subtotal is the sum of already-rounded lines (rule 7), never the rounding of a sum.
    """
    # Only a list or tuple is a sequence of lines: a dict would sum its keys and a set would
    # collapse equal line amounts (two 1-minor-unit lines would become one).
    if not isinstance(line_amounts, (list, tuple)):
        raise ValueError('line_amounts must be a list or tuple of nonnegative integers')
    lines = [_money(amount, 'line amount') for amount in line_amounts]
    _money(credit_minor, 'credit_minor')
    subtotal = sum(lines)
    applied = min(subtotal, credit_minor)
    return subtotal, applied, credit_minor - applied, subtotal - applied
