import unittest

from rvn_ledger import money
class MoneyTests(unittest.TestCase):
    def test_half_up_boundaries(self):
        self.assertEqual([money.round_half_up(n, 10000) for n in (4999, 5000, 5001)], [0, 1, 1])

    def test_large_integer_is_exact(self):
        self.assertEqual(money.round_half_up(10**30 + 5000, 10000), 10**26 + 1)

    def test_subscription_proration(self):
        self.assertEqual(money.subscription_amount(2900, 16, 30), 1547)
        self.assertEqual(money.subscription_amount(2900, 30, 30), 2900)
        self.assertEqual(money.subscription_amount(2900, 0, 30), 0)

    def test_lines_round_before_credit(self):
        # Review finding 8, using only the existing helpers. Source rule 7: a usage line is
        # round_half_up(units * unit_price_micros / 10000) and the subtotal is the sum of the
        # separately rounded lines; credit applies to that subtotal. Two lines worth exactly
        # half a minor unit each make the order observable: per-line rounding gives 1 + 1 = 2,
        # rounding the raw sum would give 1, and a 1-unit credit then leaves 1 versus 0.
        raw_lines = [(1, 5000), (1, 5000)]  # (units, unit_price_micros)
        per_line = [money.round_half_up(units * price, 10000) for units, price in raw_lines]
        self.assertEqual(per_line, [1, 1])
        self.assertEqual(money.apply_credit(per_line, 1), (2, 1, 0, 1))
        rounded_sum = money.round_half_up(sum(units * price for units, price in raw_lines), 10000)
        self.assertEqual(rounded_sum, 1)
        self.assertEqual(money.apply_credit([rounded_sum], 1), (1, 1, 0, 0))
        self.assertNotEqual(money.apply_credit(per_line, 1), money.apply_credit([rounded_sum], 1))
        # An unrounded amount can never reach the credit step: apply_credit refuses anything
        # that is not already an integer minor-unit amount, so rounding cannot be deferred.
        from decimal import Decimal
        from fractions import Fraction
        for unrounded in ([Fraction(1, 2), Fraction(1, 2)], [0.5, 0.5], [Decimal('0.5'), Decimal('0.5')], [1, Fraction(1, 2)]):
            with self.subTest(unrounded=unrounded), self.assertRaises(ValueError):
                money.apply_credit(unrounded, 1)

    def test_credit_capped(self):
        self.assertEqual(money.apply_credit([10, 20], 50), (30, 30, 20, 0))

    def test_no_usage_still_has_subscription(self):
        self.assertEqual(money.apply_credit([money.subscription_amount(2900, 30, 30)], 0), (2900, 0, 0, 2900))

    def test_invalid_money_inputs_rejected(self):
        for n in (True, 1.5, -1):
            with self.subTest(n=n), self.assertRaises(ValueError):
                money.round_half_up(n, 10000)
        with self.assertRaises(ValueError):
            money.round_half_up(1, 0)

    def test_credit_monotonic_property(self):
        totals = [money.apply_credit([17, 24], c)[3] for c in range(100)]
        self.assertTrue(all(a >= b >= 0 for a, b in zip(totals, totals[1:])))

    # Review finding 3: every money entry point enforces "integer, nonnegative, no floats".
    def test_apply_credit_rejects_non_money_values(self):
        for lines, credit in [([10], -5), ([10], 2.5), ([10], True), ([10, -1], 0), ([1.0], 0),
                              ([True], 0), (['10'], 0), ([10], '5'), ([10], None), (10, 0)]:
            with self.subTest(lines=lines, credit=credit), self.assertRaises(ValueError):
                money.apply_credit(lines, credit)
        self.assertEqual(money.apply_credit([], 5), (0, 0, 5, 0))
        self.assertEqual(money.apply_credit((10, 20), 5), (30, 5, 0, 25))

    def test_apply_credit_takes_only_a_sequence_of_line_amounts(self):
        # Reviewer hypothesis (round 3): a dict would sum its keys and a set would collapse equal
        # line amounts (two 1-minor-unit lines become one). Only a list or tuple of lines is money.
        for lines in ({10: 'x', 20: 'y'}, {10, 10}, frozenset({10}), iter([10]), (x for x in [10]), range(3)):
            with self.subTest(lines=lines), self.assertRaises(ValueError):
                money.apply_credit(lines, 0)

    def test_subscription_amount_rejects_invalid_days_and_fees(self):
        for fee, days, period_days in [(2900, True, 30), (2900, 31, 30), (2900, -1, 30), (2900, 1.0, 30),
                                       (2900.0, 1, 30), (-2900, 1, 30), (True, 1, 30), (2900, 1, 0),
                                       (2900, 1, True), (2900, 1, -30), ('2900', 1, 30)]:
            with self.subTest(fee=fee, days=days, period_days=period_days), self.assertRaises(ValueError):
                money.subscription_amount(fee, days, period_days)
