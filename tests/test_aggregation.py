import unittest
from datetime import datetime, timezone
from rvn_ledger.inputs import EventRow
from rvn_ledger.aggregation import aggregate_usage


def event_row(n, account='a', metric='api_calls', units=2, event_id=None):
    return EventRow(n, str(n), dict(event_id=event_id or f'e{n}', account_id=account, metric=metric, units=units), None)


def row(n, account='a', metric='api_calls', units=2, event_id=None):
    # Review finding 4: aggregation only takes AcceptedEvent values, which only the
    # classification glue produces; the test fixture states that acceptance explicitly.
    from rvn_ledger.selection import AcceptedEvent
    instant = datetime(2026, 9, 10, tzinfo=timezone.utc)
    return AcceptedEvent(event_row(n, account, metric, units, event_id), instant, instant)


class AggregationTests(unittest.TestCase):
    def test_groups_and_preserves_sources(self):
        result = aggregate_usage([row(3, units=7), row(1, units=5), row(2, metric='tokens', units=11)], ['a', 'b'], ['api_calls', 'tokens'])
        self.assertEqual(result['a']['api_calls']['units'], 12)
        self.assertEqual(result['a']['api_calls']['sources'], [{'event_id': 'e1', 'line_number': 1, 'sha256': '1', 'units': 5}, {'event_id': 'e3', 'line_number': 3, 'sha256': '3', 'units': 7}])
        self.assertEqual(result['a']['tokens']['units'], 11)
        self.assertEqual(result['b']['tokens'], {'units': 0, 'sources': []})

    def test_order_does_not_change_result(self):
        rows = [row(3), row(1), row(2)]
        self.assertEqual(aggregate_usage(rows, ['b', 'a'], ['tokens', 'api_calls']), aggregate_usage(rows[::-1], ['a', 'b'], ['api_calls', 'tokens']))

    def test_key_order_is_sorted_regardless_of_input_order(self):
        # Review finding 6: dict equality ignores key order, so check the order itself.
        result = aggregate_usage([row(1)], ['b', 'a'], ['tokens', 'api_calls'])
        self.assertEqual(list(result), ['a', 'b'])
        self.assertEqual([list(groups) for groups in result.values()], [['api_calls', 'tokens'], ['api_calls', 'tokens']])

    def test_rejects_invalid_or_duplicate_inputs(self):
        for rows in [[row(1, account='unknown')], [row(1, metric='unknown')], [row(1, units=True)], [row(1, units=0)], [row(1), row(2, event_id='e1')]]:
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                aggregate_usage(rows, ['a'], ['api_calls'])

    def test_iterator_inputs_are_read_once_and_ids_are_validated(self):
        # Review N3: `metrics`/`account_ids` are consumed exactly once, so an iterator
        # gives every account the same metric map; repeated, empty, non-string or
        # non-collection ids are refused instead of collapsing silently.
        empty = {'units': 0, 'sources': []}
        self.assertEqual(aggregate_usage([], ['a', 'b'], iter(['api_calls'])),
                         {'a': {'api_calls': empty}, 'b': {'api_calls': empty}})
        result = aggregate_usage([row(1)], iter(['b', 'a']), iter(['tokens', 'api_calls']))
        self.assertEqual([list(result)] + [list(groups) for groups in result.values()], [['a', 'b'], ['api_calls', 'tokens'], ['api_calls', 'tokens']])
        self.assertEqual(result['a']['api_calls']['units'], 2)
        for account_ids, metrics in [(['a', 'a'], ['api_calls']), (['a'], ['api_calls', 'api_calls']), (['a', 3], ['api_calls']),
                                     (['a'], ['']), (['a'], [None]), ('a', ['api_calls']), (['a'], 'api_calls'), (['a'], None), (None, ['api_calls'])]:
            with self.subTest(account_ids=account_ids, metrics=metrics), self.assertRaises(ValueError):
                aggregate_usage([], account_ids, metrics)

    def test_plain_rows_are_not_accepted_events(self):
        # Review finding 4 reproducer: an out-of-period EventRow must not be countable.
        stale = EventRow(1, 'h', {'event_id': 'e', 'account_id': 'a', 'metric': 'm', 'units': 5, 'ts': '2020-01-01T00:00:00Z'}, None)
        for bad in [[stale], [event_row(1)], [row(1), event_row(2)], [{'row': event_row(1)}]]:
            with self.subTest(bad=bad), self.assertRaises(TypeError):
                aggregate_usage(bad, ['a'], ['api_calls', 'm'])

    def test_large_integer_exactness(self):
        result = aggregate_usage([row(1, units=10**30), row(2, units=1)], ['a'], ['api_calls'])
        self.assertEqual(result['a']['api_calls']['units'], 10**30 + 1)

    def test_generated_conservation(self):
        import random
        rng = random.Random(73)
        rows = [row(n, account=rng.choice(['a', 'b']), metric=rng.choice(['api_calls', 'tokens']), units=rng.randint(1, 10000)) for n in range(1, 201)]
        result = aggregate_usage(rows, ['a', 'b'], ['api_calls', 'tokens'])
        self.assertEqual(sum(r.row.value['units'] for r in rows), sum(g['units'] for groups in result.values() for g in groups.values()))
        self.assertEqual(sorted(r.row.line_number for r in rows), sorted(s['line_number'] for groups in result.values() for g in groups.values() for s in g['sources']))
