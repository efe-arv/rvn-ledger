import random
import unittest

from rvn_ledger.inputs import InputError

from fixtures import TIER_PLANS as PLANS, reference_line


def reference_split(units, tiers):
    """Independent cumulative bracket split: [(from, to, units_in_tier), ...] for tiers that carry units."""
    out, position = [], 0
    for tier in tiers:
        upper = tier['to_units']
        take = units - position if upper is None else max(0, min(units, upper) - position)
        if take > 0:
            out.append((tier['from_units'], upper, take))
        position = position if upper is None else max(position, min(units, upper))
    return out


class TierTests(unittest.TestCase):
    def price(self, units, plan='growth', currency='USD', metric='api_calls', plans=PLANS):
        from rvn_ledger.tiers import metric_tiers, price_usage
        return [(l.tier_from, l.tier_to, l.units, l.amount_minor) for l in price_usage(units, metric_tiers(plans, plan, currency, metric), metric)]

    def test_each_boundary_below_at_and_above(self):
        # Growth api_calls brackets [0,50000) [50000,250000) [250000,None): units below, at and just past each boundary.
        self.assertEqual(self.price(49999), [(0, 50000, 49999, reference_line(49999, 1900))])
        self.assertEqual(self.price(50000), [(0, 50000, 50000, 9500)])
        self.assertEqual(self.price(50001), [(0, 50000, 50000, 9500), (50000, 250000, 1, reference_line(1, 1400))])
        self.assertEqual(self.price(250000), [(0, 50000, 50000, 9500), (50000, 250000, 200000, 28000)])
        self.assertEqual(self.price(250001), [(0, 50000, 50000, 9500), (50000, 250000, 200000, 28000), (250000, None, 1, 0)])
        self.assertEqual(self.price(1), [(0, 50000, 1, 0)])  # 0.19 minor units rounds to 0 but the line still exists

    def test_usage_spanning_several_tiers_is_cumulative_over_the_period(self):
        self.assertEqual(self.price(300000), [(0, 50000, 50000, 9500), (50000, 250000, 200000, 28000), (250000, None, 50000, 4500)])
        self.assertEqual(self.price(12005, 'starter', 'TRY'), [(0, 10000, 10000, 86000), (10000, None, 2005, 12231)])  # 12230.5 -> 12231

    def test_half_minor_unit_rounds_up_per_line(self):
        # 3 units * 1900 micros = 5700 micros = 0.57 minor -> 1; 1 unit = 0.19 -> 0; 5 units * 1000 micros = 0.5 -> 1.
        self.assertEqual(self.price(3), [(0, 50000, 3, 1)])
        plans = {'p': {'plan_id': 'p', 'prices': {'USD': {'subscription_fee_minor': 0, 'metrics': {
            'api_calls': [{'from_units': 0, 'to_units': None, 'unit_price_micros': 1000}]}}}}}
        self.assertEqual(self.price(5, 'p', plans=plans), [(0, None, 5, 1)])
        self.assertEqual(self.price(4, 'p', plans=plans), [(0, None, 4, 0)])
        self.assertEqual(self.price(15, 'p', plans=plans), [(0, None, 15, 2)])

    def test_zero_usage_produces_no_line(self):
        self.assertEqual(self.price(0), [])
        self.assertEqual(self.price(0, 'starter', 'TRY', 'storage_gb_hours'), [])

    def test_lines_are_priced_in_the_requested_currency_only(self):
        self.assertEqual(self.price(100, 'starter', 'USD'), [(0, 10000, 100, 25)])
        self.assertEqual(self.price(100, 'starter', 'TRY'), [(0, 10000, 100, 860)])
        with self.assertRaises(InputError):
            self.price(100, 'growth', 'TRY')  # growth has no TRY tariff in this fixture: never invent one

    def test_amounts_are_ints_and_units_conserved(self):
        from rvn_ledger.tiers import metric_tiers, price_usage
        tiers = metric_tiers(PLANS, 'growth', 'USD', 'api_calls')
        rng = random.Random(20260930)
        for _ in range(300):
            units = rng.choice([rng.randrange(0, 600000), rng.choice([49999, 50000, 50001, 249999, 250000, 250001])])
            lines = price_usage(units, tiers, 'api_calls')
            with self.subTest(units=units):
                self.assertEqual(sum(l.units for l in lines), units)
                self.assertTrue(all(type(l.units) is int and type(l.amount_minor) is int and l.units > 0 for l in lines))
                self.assertEqual([(l.tier_from, l.tier_to, l.units) for l in lines], reference_split(units, [
                    {'from_units': 0, 'to_units': 50000}, {'from_units': 50000, 'to_units': 250000}, {'from_units': 250000, 'to_units': None}]))
                prices = {0: 1900, 50000: 1400, 250000: 900}
                self.assertEqual([l.amount_minor for l in lines], [reference_line(l.units, prices[l.tier_from]) for l in lines])
                self.assertTrue(all(l.tier_to is None or l.tier_from < l.tier_to for l in lines))

    def test_invalid_units_are_refused(self):
        from rvn_ledger.tiers import metric_tiers, price_usage
        tiers = metric_tiers(PLANS, 'growth', 'USD', 'api_calls')
        for units in (-1, True, 1.0, '5', None):
            with self.subTest(units=units), self.assertRaises(ValueError):
                price_usage(units, tiers, 'api_calls')

    def test_unusable_tariffs_fail_closed(self):
        from rvn_ledger.tiers import metric_tiers, tiers_from_document

        def plans_with(tiers, metric='api_calls'):
            return {'p': {'plan_id': 'p', 'prices': {'USD': {'subscription_fee_minor': 0, 'metrics': {metric: tiers}}}}}
        good = [{'from_units': 0, 'to_units': 10, 'unit_price_micros': 5}, {'from_units': 10, 'to_units': None, 'unit_price_micros': 4}]
        self.assertEqual(metric_tiers(plans_with(good), 'p', 'USD', 'api_calls'), tiers_from_document(good))
        bad = [
            [],                                                                                     # no tiers
            [{'from_units': 1, 'to_units': None, 'unit_price_micros': 5}],                          # not starting at 0
            [{'from_units': 0, 'to_units': 10, 'unit_price_micros': 5}],                            # last tier bounded
            [{'from_units': 0, 'to_units': 10, 'unit_price_micros': 5}, {'from_units': 11, 'to_units': None, 'unit_price_micros': 4}],  # gap
            [{'from_units': 0, 'to_units': 10, 'unit_price_micros': 5}, {'from_units': 9, 'to_units': None, 'unit_price_micros': 4}],   # overlap
            [{'from_units': 0, 'to_units': None, 'unit_price_micros': 5}, {'from_units': 10, 'to_units': None, 'unit_price_micros': 4}],  # open tier not last
            [{'from_units': 0, 'to_units': 0, 'unit_price_micros': 5}, {'from_units': 0, 'to_units': None, 'unit_price_micros': 4}],     # empty bracket
            [{'from_units': 0, 'to_units': None, 'unit_price_micros': -1}],                          # negative price
            [{'from_units': 0, 'to_units': None, 'unit_price_micros': 1.5}],                         # float price
            [{'from_units': 0, 'to_units': None, 'unit_price_micros': True}],                        # bool price
            [{'from_units': 0, 'to_units': None, 'unit_price_micros': '5'}],                         # string price
            [{'from_units': 0, 'to_units': None}],                                                   # missing price
            [{'from_units': 0.0, 'to_units': None, 'unit_price_micros': 5}],                         # float bound
            [{'from_units': 0, 'to_units': True, 'unit_price_micros': 5}, {'from_units': 1, 'to_units': None, 'unit_price_micros': 5}],  # bool bound
            [{'from_units': 0, 'to_units': '10', 'unit_price_micros': 5}],                           # string bound
            ['tier'],                                                                               # not an object
            'tiers',                                                                                # not a list
            None,
        ]
        for tiers in bad:
            with self.subTest(tiers=tiers), self.assertRaises(InputError):
                metric_tiers(plans_with(tiers), 'p', 'USD', 'api_calls')
            with self.subTest(audit_tiers=tiers), self.assertRaises(InputError):
                tiers_from_document(tiers)
        for plans, plan, currency, metric in [
            (plans_with(good), 'p', 'USD', 'storage_gb_hours'),   # metric without a tariff
            (plans_with(good), 'p', 'EUR', 'api_calls'),          # currency without prices
            (plans_with(good), 'q', 'USD', 'api_calls'),          # unknown plan
            ({'p': {'plan_id': 'p', 'prices': {'USD': {'subscription_fee_minor': 0}}}}, 'p', 'USD', 'api_calls'),  # no metrics
            ({'p': {'plan_id': 'p', 'prices': {'USD': {'subscription_fee_minor': 0, 'metrics': []}}}}, 'p', 'USD', 'api_calls'),
            ([], 'p', 'USD', 'api_calls'),
        ]:
            with self.subTest(plan=plan, currency=currency, metric=metric), self.assertRaises(InputError):
                metric_tiers(plans, plan, currency, metric)
