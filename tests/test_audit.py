import copy
import hashlib
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from fixtures import (ACCOUNTS, EXPECTED_INVOICES, EXPECTED_QUARANTINE, INPUT_NAMES, PERIOD, PLANS, RAW_EVENTS, event, publish_run,
                      raw_lines, rewrite_manifest, run_cli, run_fixture, SCRATCH, write_fixture)


class AuditTests(unittest.TestCase):
    def test_every_raw_line_has_one_decision_with_source_hash(self):
        run = run_fixture()
        decisions = run.audit['decisions']
        self.assertEqual([d['line'] for d in decisions], list(range(1, 19)))
        self.assertEqual({d['status'] for d in decisions} <= {'accepted', 'duplicate_ignored', 'excluded_out_of_period', 'excluded_late', 'quarantined'}, True)
        self.assertEqual(decisions[2], {'line': 3, 'sha256': decisions[2]['sha256'], 'event_id': 'a2', 'status': 'duplicate_ignored', 'canonical_line': 2})
        self.assertEqual(decisions[7]['reasons'], ['nonpositive_units'])
        self.assertEqual(decisions[17], {'line': 18, 'sha256': decisions[17]['sha256'], 'event_id': None, 'status': 'quarantined', 'reasons': ['invalid_json']})
        self.assertEqual(len(decisions[0]['sha256']), 64)
        counts = run.manifest['counts']
        self.assertEqual(counts['raw'], 18)
        self.assertEqual(sum(counts[k] for k in ('accepted', 'duplicate_ignored', 'excluded_out_of_period', 'excluded_late', 'quarantined')), 18)

    def test_every_usage_line_traces_to_events_whose_units_reproduce_it(self):
        run = run_fixture()
        invoices = {inv['account_id']: inv for inv in run.invoices}
        for account_id, trail in run.audit['invoices'].items():
            for metric, sources in trail['usage_sources'].items():
                total = sum(s['units'] for s in sources)
                self.assertEqual(total, invoices[account_id]['billable_units'][metric])
                lines = [l for l in trail['usage_lines'] if l['metric'] == metric]
                self.assertEqual(sum(l['units'] for l in lines), total)
                for line in lines:
                    self.assertEqual(line['plan_id'], trail['period_end_plan_id'])
                    self.assertIn('unit_price_micros', line)
                    self.assertEqual(line['formula'], f"round_half_up({line['units']} * {line['unit_price_micros']} / 10000) = {line['amount_minor']}")
            for line in trail['subscription_lines']:
                self.assertEqual(line['formula'], f"round_half_up({line['fee_minor']} * {line['days']} / {line['days_in_period']}) = {line['amount_minor']}")
                self.assertIn('from', line)
                self.assertIn('to', line)
        a_sources = run.audit['invoices']['acct_a']['usage_sources']['api_calls']
        self.assertEqual([(s['event_id'], s['line'], s['units']) for s in a_sources], [('a1', 1, 4000), ('a2', 2, 8000), ('a6', 7, 5)])
        self.assertEqual(run.audit['invoices']['acct_a']['quarantined_lines'], [8, 9])
        self.assertEqual(run.audit['invoices']['acct_a']['credit'], {'credit_minor': 500, 'applied_minor': 500, 'remaining_minor': 0})

    def test_reconcile_recomputes_every_invoice_from_the_trail(self):
        from rvn_ledger.audit import AuditError, reconcile
        run = run_fixture()
        reconcile(run.invoices, run.quarantine, run.audit, run.manifest)
        tampered = [
            ('invoice amount', lambda r: r.invoices[0]['lines'][1].__setitem__('amount_minor', 86001)),
            # subtotal unchanged: only the line-by-line comparison with the trail can see this one
            ('amount moved between lines', lambda r: (r.invoices[0]['lines'][1].__setitem__('amount_minor', 86001),
                                                      r.invoices[0]['lines'][2].__setitem__('amount_minor', 12230))),
            # sums unchanged: only the every-accepted-event-used-exactly-once check can see this one
            ('source reused across metrics', lambda r: r.audit['invoices']['acct_a']['usage_sources']['storage_gb_hours'].__setitem__(
                0, {'event_id': 'a1', 'line': 1, 'units': 100})),
            ('invoice total', lambda r: r.invoices[3].__setitem__('total_minor', 9650)),
            ('source units', lambda r: r.audit['invoices']['acct_a']['usage_sources']['api_calls'][0].__setitem__('units', 4001)),
            ('dropped source', lambda r: r.audit['invoices']['acct_a']['usage_sources']['api_calls'].pop()),
            ('tier price', lambda r: r.audit['invoices']['acct_b']['usage_lines'][0].__setitem__('unit_price_micros', 1901)),
            ('segment days', lambda r: r.audit['invoices']['acct_b']['subscription_lines'][0].__setitem__('days', 17)),
            ('credit', lambda r: r.invoices[2].__setitem__('credit_applied_minor', 27899)),
            ('quarantined count', lambda r: r.invoices[0].__setitem__('quarantined_count', 3)),
            ('quarantine entry', lambda r: r.quarantine.pop()),
            ('decision', lambda r: r.audit['decisions'][0].__setitem__('status', 'quarantined')),
            ('manifest count', lambda r: r.manifest['counts'].__setitem__('accepted', 1)),
            ('billable units', lambda r: r.invoices[1]['billable_units'].__setitem__('api_calls', 60001)),
            ('currency swap', lambda r: r.invoices[0].__setitem__('currency', 'USD')),
        ]
        for name, mutate in tampered:
            broken = copy.deepcopy(run)
            mutate(broken)
            with self.subTest(tamper=name), self.assertRaises(AuditError):
                reconcile(broken.invoices, broken.quarantine, broken.audit, broken.manifest)

    def test_quarantine_entries_are_in_source_order_with_primary_reason(self):
        run = run_fixture()
        self.assertEqual(run.quarantine, EXPECTED_QUARANTINE)
        self.assertEqual(run.invoices, EXPECTED_INVOICES)
        self.assertEqual(run.manifest['quarantine_reasons'],
                         {'invalid_json': 1, 'invalid_ts': 1, 'invalid_units': 1, 'nonpositive_units': 2, 'unknown_account': 1, 'unknown_metric': 1})

    def test_shifted_tier_units_rejected_even_with_consistent_amounts(self):
        from rvn_ledger.audit import AuditError, reconcile
        run = run_fixture()
        inv = run.invoices[0]
        tiers = [u for u in run.audit['invoices'][inv['account_id']]['usage_lines'] if u['metric'] == 'api_calls']
        self.assertGreaterEqual(len(tiers), 2)
        old_subtotal = inv['subtotal_minor']
        for u, delta in zip(tiers[:2], (-1000, 1000)):
            u['units'] += delta
            u['amount_minor'] = (2 * u['units'] * u['unit_price_micros'] + 10000) // 20000
            u['formula'] = f"round_half_up({u['units']} * {u['unit_price_micros']} / 10000) = {u['amount_minor']}"
            line = next(l for l in inv['lines'] if l.get('metric') == 'api_calls' and l.get('tier_from') == u['tier_from'])
            line.update(units=u['units'], amount_minor=u['amount_minor'])
        inv['subtotal_minor'] = sum(l['amount_minor'] for l in inv['lines'])
        inv['total_minor'] = inv['subtotal_minor'] - inv['credit_applied_minor']
        bucket = run.manifest['totals_by_currency'][inv['currency']]
        bucket['subtotal_minor'] += inv['subtotal_minor'] - old_subtotal
        bucket['total_minor'] += inv['subtotal_minor'] - old_subtotal
        with self.assertRaises(AuditError):
            reconcile(run.invoices, run.quarantine, run.audit, run.manifest)
        # Re-hash the forged documents: check must reject at reconciliation, not merely hashes.
        from rvn_ledger.outputs import publish, serialize
        with tempfile.TemporaryDirectory() as tmp:
            publish(tmp, {n: serialize(d) for n, d in [('invoices.json', run.invoices),
                    ('quarantine.json', run.quarantine), ('audit.json', run.audit)]}, run.manifest)
            result = run_cli('check', '--out', tmp)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertNotIn('Traceback', result.stderr)

    def test_duplicate_target_and_formula_corruption_rejected(self):
        from rvn_ledger.audit import AuditError, reconcile
        for mutate in (lambda r: r.audit['decisions'][2].update(canonical_line=999999),
                       lambda r: r.audit['invoices']['acct_a']['subscription_lines'][0].update(formula='wrong')):
            run = run_fixture()
            mutate(run)
            with self.assertRaises(AuditError):
                reconcile(run.invoices, run.quarantine, run.audit, run.manifest)

    def test_manifest_nested_container_types_are_validated(self):
        from rvn_ledger.outputs import publish, serialize
        for field in ('counts', 'quarantine_reasons', 'totals_by_currency', 'inputs', 'rules', 'versions'):
            run = run_fixture()
            run.manifest[field] = None
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                publish(tmp, {'invoices.json': serialize(run.invoices), 'quarantine.json': serialize(run.quarantine),
                              'audit.json': serialize(run.audit)}, run.manifest)
                result = run_cli('check', '--out', tmp)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertNotIn('Traceback', result.stderr)

    def test_bad_nested_audit_is_controlled_cli_error(self):
        from rvn_ledger.outputs import publish, serialize
        for field in ('decisions', 'invoices'):
            run = run_fixture()
            run.audit[field] = None
            with tempfile.TemporaryDirectory() as tmp:
                publish(tmp, {'invoices.json': serialize(run.invoices), 'quarantine.json': serialize(run.quarantine),
                              'audit.json': serialize(run.audit)}, run.manifest)
                result = run_cli('check', '--out', tmp)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertNotIn('Traceback', result.stderr)


class OutputValidationTests(unittest.TestCase):
    """Rehashed invalid documents must fail reconciliation, check and explain."""

    def empty_run(self):
        from rvn_ledger.pipeline import run_ledger
        first = copy.deepcopy(ACCOUNTS[-1])
        first['credit_minor'] = 0
        accounts = [first, {**copy.deepcopy(first), 'account_id': 'acct_e'}]
        return run_ledger({'events.jsonl': b'', 'accounts.json': json.dumps(accounts).encode(),
                           'plans.json': json.dumps(PLANS).encode(), 'period.json': json.dumps(PERIOD).encode()})

    def rejected(self, run, cli=False):
        from rvn_ledger.audit import AuditError, reconcile
        with self.assertRaises(AuditError):
            reconcile(run.invoices, run.quarantine, run.audit, run.manifest)
        if cli:
            with tempfile.TemporaryDirectory(dir=SCRATCH) as tmp:
                documents = {'invoices.json': run.invoices, 'quarantine.json': run.quarantine, 'audit.json': run.audit}
                manifest = copy.deepcopy(run.manifest)
                manifest['outputs'] = {}
                for name, value in documents.items():
                    raw = (json.dumps(value) + '\n').encode()
                    (Path(tmp) / name).write_bytes(raw)
                    manifest['outputs'][name] = {'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)}
                # Bypass the writer's type checks to exercise malformed published documents.
                (Path(tmp) / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
                for command in ('check', 'explain'):
                    result = run_cli(command, '--out', tmp, '--json')
                    self.assertEqual(result.returncode, 2, (command, result.stdout, result.stderr))
                    self.assertEqual(result.stdout, '')
                    self.assertIn('check failed:', result.stderr)
                    self.assertNotIn('Traceback', result.stderr)

    def test_invoice_accounts_are_unique_and_match_the_audit(self):
        from rvn_ledger.audit import reconcile
        good = self.empty_run()
        reconcile(good.invoices, good.quarantine, good.audit, good.manifest)
        broken = copy.deepcopy(good)
        broken.invoices[1]['account_id'] = broken.invoices[0]['account_id']
        self.rejected(broken, cli=True)
        broken = copy.deepcopy(good)
        broken.audit['invoices']['unbilled'] = broken.audit['invoices'].pop('acct_e')
        self.rejected(broken)

    def test_duplicate_target_must_have_entered_canonical_selection(self):
        from rvn_ledger.audit import reconcile
        from rvn_ledger.pipeline import run_ledger
        # The valid winner is later in the file; an invalid sequence never competes with it.
        values = [event('same', 'bad', 'acct_a'), event('same', 3, 'acct_a'), event('same', 2, 'acct_a')]
        run = run_ledger({'events.jsonl': raw_lines(values), 'accounts.json': json.dumps(ACCOUNTS).encode(),
                          'plans.json': json.dumps(PLANS).encode(), 'period.json': json.dumps(PERIOD).encode()})
        self.assertEqual(run.audit['decisions'][1]['canonical_line'], 3)
        reconcile(run.invoices, run.quarantine, run.audit, run.manifest)
        run.audit['decisions'][1]['canonical_line'] = 1
        self.rejected(run, cli=True)

    def test_all_numeric_output_fields_reject_equal_floats_and_booleans(self):
        def numeric_paths(value, path=()):
            if type(value) is int:
                yield path, value
            elif isinstance(value, dict):
                for key, child in value.items():
                    yield from numeric_paths(child, (*path, key))
            elif isinstance(value, list):
                for index, child in enumerate(value):
                    yield from numeric_paths(child, (*path, index))

        good = run_fixture()
        for name in ('invoices', 'audit', 'manifest'):
            for path, value in numeric_paths(getattr(good, name)):
                replacements = [float(value), *([bool(value)] if value in (0, 1) else [])]
                for replacement in replacements:
                    broken = copy.deepcopy(good)
                    target = getattr(broken, name)
                    for part in path[:-1]:
                        target = target[part]
                    target[path[-1]] = replacement
                    with self.subTest(document=name, path=path, replacement=replacement):
                        self.rejected(broken)

    def test_published_money_types_fail_check_and_explain(self):
        cases = (
            lambda r: r.invoices[0].update(total_minor=float(r.invoices[0]['total_minor'])),
            lambda r: r.invoices[0].update(subtotal_minor=float(r.invoices[0]['subtotal_minor'])),
            lambda r: r.invoices[0].update(credit_applied_minor=False),
            lambda r: r.audit['invoices'][r.invoices[0]['account_id']]['credit'].update(applied_minor=False),
            lambda r: r.manifest['totals_by_currency']['USD'].update(total_minor=float(r.manifest['totals_by_currency']['USD']['total_minor'])),
        )
        for index, mutate in enumerate(cases):
            run = self.empty_run()
            mutate(run)
            with self.subTest(case=index):
                self.rejected(run, cli=True)

    def test_unused_audit_tariffs_are_validated(self):
        good = self.empty_run()
        account = good.invoices[0]['account_id']
        cases = (
            [{'from_units': 5, 'to_units': None, 'unit_price_micros': -1}],
            [{'from_units': 0, 'to_units': None, 'unit_price_micros': -1}],
            [{'from_units': 0, 'to_units': 10, 'unit_price_micros': 1}],
            [{'from_units': 0, 'to_units': 10, 'unit_price_micros': 1},
             {'from_units': 11, 'to_units': None, 'unit_price_micros': 1}],
        )
        for tiers in cases:
            run = copy.deepcopy(good)
            run.audit['invoices'][account]['usage_tariffs']['api_calls']['tiers'] = tiers
            with self.subTest(tiers=tiers):
                self.rejected(run, cli=True)


class ManifestInputMetadataTests(unittest.TestCase):
    def rejected(self, mutate, case):
        from rvn_ledger.audit import AuditError, reconcile
        run = run_fixture()
        mutate(run.manifest)
        with self.subTest(case=case), self.assertRaises(AuditError):
            reconcile(run.invoices, run.quarantine, run.audit, run.manifest)

    def test_actual_manifest_reconciles_and_has_the_expected_schema(self):
        from rvn_ledger.audit import reconcile
        run = run_fixture()
        self.assertEqual(tuple(run.manifest['inputs']), INPUT_NAMES)
        for name, meta in run.manifest['inputs'].items():
            self.assertEqual(set(meta), {'sha256', 'bytes', 'records'}, name)
        reconcile(run.invoices, run.quarantine, run.audit, run.manifest)

    def test_every_required_input_entry_is_required_and_nothing_else_is_allowed(self):
        for name in INPUT_NAMES:
            self.rejected(lambda m, name=name: m['inputs'].pop(name), f'missing {name}')
            self.rejected(lambda m, name=name: m['inputs'].__setitem__(name, None), f'null {name}')
        self.rejected(lambda m: m['inputs'].__setitem__('extra.json', dict(m['inputs']['period.json'])), 'extra input name')
        self.rejected(lambda m: m['inputs'].__setitem__('Events.jsonl', m['inputs'].pop('events.jsonl')), 'renamed input')
        self.rejected(lambda m: m.__setitem__('inputs', {}), 'empty inputs')
        self.rejected(lambda m: m.__setitem__('inputs', []), 'inputs is a list')
        self.rejected(lambda m: m.__setitem__('inputs', None), 'inputs is null')

    def test_entry_shapes_and_field_types_are_validated(self):
        run = run_fixture()
        good = run.manifest['inputs']['events.jsonl']
        digest = good['sha256']
        bad_entries = (None, [], 'text', 7, True, {}, {'sha256': digest}, {'sha256': digest, 'bytes': good['bytes']},
                       {**good, 'extra': 1}, {'sha256': digest, 'bytes': good['bytes'], 'record': good['records']})
        for name in INPUT_NAMES:
            for bad in bad_entries:
                self.rejected(lambda m, name=name, bad=bad: m['inputs'].__setitem__(name, bad), f'{name} entry {bad!r}')
        bad_values = {
            'sha256': (None, 1, True, [], 'abc', digest.upper(), digest[:-1], digest + '0', 'g' * 64, digest[:-1] + 'G'),
            'bytes': (None, True, False, -1, '12', 1.0, 12.0, [], {}),
            'records': (None, True, False, -1, '1', 1.5, 0.0, [], {}),
        }
        for field, values in bad_values.items():
            for bad in values:
                for name in INPUT_NAMES:
                    self.rejected(lambda m, name=name, field=field, bad=bad: m['inputs'][name].__setitem__(field, bad),
                                  f'{name}.{field} = {bad!r}')

    def test_record_counts_reconcile_with_the_run(self):
        # events.jsonl records are the raw lines; period.json is one object; accounts.json yields one invoice each;
        # plans.json must hold at least one plan. Each is a fact of the run, so the manifest cannot contradict it.
        self.rejected(lambda m: m['inputs']['events.jsonl'].__setitem__('records', m['counts']['raw'] + 1), 'events records != raw')
        self.rejected(lambda m: m['inputs']['events.jsonl'].__setitem__('records', 0), 'events records zero')
        self.rejected(lambda m: m['inputs']['period.json'].__setitem__('records', 2), 'period records')
        self.rejected(lambda m: m['inputs']['accounts.json'].__setitem__('records', m['counts']['invoices'] + 1), 'accounts records != invoices')
        self.rejected(lambda m: m['inputs']['plans.json'].__setitem__('records', 0), 'plans records zero')

    def test_cli_check_and_explain_fail_closed_without_an_input_entry(self):
        for name in INPUT_NAMES:
            with self.subTest(missing=name), tempfile.TemporaryDirectory(dir=SCRATCH) as tmp:
                inputs = write_fixture(Path(tmp) / 'in')
                out = Path(tmp) / 'out'
                self.assertEqual(run_cli('run', '--input-dir', inputs, '--out', out).returncode, 0)
                rewrite_manifest(out, lambda m, name=name: m['inputs'].pop(name))
                for args in (('check', '--out', out), ('check', '--out', out, '--json'),
                             ('explain', '--out', out), ('explain', '--out', out, '--json'),
                             ('explain', '--out', out, '--events', inputs / 'events.jsonl'),
                             ('explain', '--out', out, '--line', '1', '--events', inputs / 'events.jsonl')):
                    result = run_cli(*args)
                    self.assertEqual(result.returncode, 2, (args, result.stdout, result.stderr))
                    self.assertNotIn('Traceback', result.stderr, args)
                    self.assertNotIn('KeyError', result.stderr, args)
                    self.assertIn('check failed', result.stderr, args)
                    self.assertEqual(result.stdout, '', args)

    def test_cli_check_and_explain_fail_closed_on_bad_input_field_types(self):
        mutations = [
            ('null entry', lambda m: m['inputs'].__setitem__('accounts.json', None)),
            ('list entry', lambda m: m['inputs'].__setitem__('plans.json', [])),
            ('null sha256', lambda m: m['inputs']['events.jsonl'].__setitem__('sha256', None)),
            ('short sha256', lambda m: m['inputs']['events.jsonl'].__setitem__('sha256', 'abc')),
            ('bool bytes', lambda m: m['inputs']['period.json'].__setitem__('bytes', True)),
            ('negative bytes', lambda m: m['inputs']['period.json'].__setitem__('bytes', -1)),
            ('text records', lambda m: m['inputs']['accounts.json'].__setitem__('records', '4')),
            ('float records', lambda m: m['inputs']['plans.json'].__setitem__('records', 2.0)),
            ('missing records', lambda m: m['inputs']['events.jsonl'].pop('records')),
            ('events records != raw', lambda m: m['inputs']['events.jsonl'].__setitem__('records', 1)),
        ]
        with tempfile.TemporaryDirectory(dir=SCRATCH) as tmp:
            inputs = write_fixture(Path(tmp) / 'in')
            good = Path(tmp) / 'good'
            self.assertEqual(run_cli('run', '--input-dir', inputs, '--out', good).returncode, 0)
            for case, mutate in mutations:
                with self.subTest(case=case):
                    out = Path(tmp) / case.replace(' ', '-')
                    out.mkdir()
                    for name in ('invoices.json', 'quarantine.json', 'audit.json', 'manifest.json'):
                        (out / name).write_bytes((good / name).read_bytes())
                    rewrite_manifest(out, mutate)
                    for args in (('check', '--out', out), ('explain', '--out', out, '--events', inputs / 'events.jsonl')):
                        result = run_cli(*args)
                        self.assertEqual(result.returncode, 2, (args, result.stderr))
                        self.assertNotIn('Traceback', result.stderr, args)
                        self.assertIn('check failed', result.stderr, args)
            self.assertEqual(run_cli('check', '--out', good).returncode, 0)   # the untouched copy still passes

    def test_manifest_inputs_reject_missing_and_malformed_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            inputs = write_fixture(Path(tmp) / 'in')
            out = Path(tmp) / 'out'
            self.assertEqual(run_cli('run', '--input-dir', inputs, '--out', out).returncode, 0)
            original = json.loads((out / 'manifest.json').read_bytes())
            mutations = []
            for name in original['inputs']:
                missing = copy.deepcopy(original['inputs']); del missing[name]
                mutations.append(missing)
                for bad in (None, [], '', 1):
                    changed = copy.deepcopy(original['inputs']); changed[name] = bad
                    mutations.append(changed)
                for field, values in {'sha256': (None, 1, '', 'g'*64, 'a'*63, 'A'*64),
                                      'bytes': (None, True, -1, 1.5, '1'),
                                      'records': (None, False, -1, 1.5, '1')}.items():
                    changed = copy.deepcopy(original['inputs']); del changed[name][field]
                    mutations.append(changed)
                    for bad in values:
                        changed = copy.deepcopy(original['inputs']); changed[name][field] = bad
                        mutations.append(changed)
            mutations.extend((None, [], {}, {**original['inputs'], 'extra.json': original['inputs']['period.json']}))
            for metadata in mutations:
                with self.subTest(metadata=metadata):
                    (out / 'manifest.json').write_text(json.dumps({**original, 'inputs': metadata}))
                    for command in (('check', '--out', out), ('explain', '--out', out, '--events', inputs / 'events.jsonl')):
                        result = run_cli(*command)
                        self.assertNotEqual(result.returncode, 0)
                        self.assertNotIn('Traceback', result.stderr)


class PeriodAndIdentityConsistencyTests(unittest.TestCase):
    """External review (2026-10-02) F1 and F2: a document set that contradicts its own manifest rules,
    or bills one event identity through two canonical lines, must fail reconciliation and `check`.
    Every tamper below is re-hashed through `publish`, so only reconciliation can refuse it."""

    def rejected(self, mutate, case, cli=False):
        from rvn_ledger.audit import AuditError, reconcile
        run = run_fixture()
        mutate(run)
        with self.subTest(case=case):
            with self.assertRaises(AuditError):
                reconcile(run.invoices, run.quarantine, run.audit, run.manifest)
            if cli:
                with tempfile.TemporaryDirectory(dir=SCRATCH) as tmp:
                    publish_run(run, tmp)
                    result = run_cli('check', '--out', tmp, '--json')
                    self.assertEqual(result.returncode, 2, (result.stdout, result.stderr))
                    self.assertNotIn('Traceback', result.stderr)
                    self.assertIn('check failed', result.stderr)
                    self.assertEqual(result.stdout, '')

    # ---- F1: the audit period, the manifest rules and the trail must describe one billing period ----

    def test_subscription_segments_must_lie_inside_the_manifest_period(self):
        def shift(run, days):
            for trail in run.audit['invoices'].values():
                for seg in trail['subscription_lines']:
                    for key in ('from', 'to'):
                        seg[key] = (date.fromisoformat(seg[key]) + timedelta(days=days)).isoformat()
        self.rejected(lambda r: shift(r, 365), 'review example 1: segments one year late', cli=True)
        self.rejected(lambda r: shift(r, -365), 'segments one year early')
        self.rejected(lambda r: shift(r, 1), 'segments end one day after the period')
        self.rejected(lambda r: shift(r, -1), 'segments start one day before the period')

    def test_manifest_rules_must_be_a_valid_period_that_agrees_with_the_documents(self):
        def rules(run):
            return run.manifest['rules']
        cases = [
            ('invented metric', lambda r: rules(r).__setitem__('metrics', ['invented'])),
            ('metrics reordered', lambda r: rules(r).__setitem__('metrics', ['storage_gb_hours', 'api_calls'])),
            ('metric dropped', lambda r: rules(r).__setitem__('metrics', ['api_calls'])),
            ('metric repeated', lambda r: rules(r).__setitem__('metrics', ['api_calls', 'storage_gb_hours', 'api_calls'])),
            ('empty metrics', lambda r: rules(r).__setitem__('metrics', [])),
            ('days_in_period 1', lambda r: rules(r).__setitem__('days_in_period', 1)),
            ('days_in_period 31', lambda r: rules(r).__setitem__('days_in_period', 31)),
            ('period ends before it starts', lambda r: rules(r).__setitem__('period_end_local_exclusive', '1900-01-01')),
            ('non-canonical period start', lambda r: rules(r).__setitem__('period_start_local', '20260901')),
            ('period start not a string', lambda r: rules(r).__setitem__('period_start_local', None)),
            ('negative late cutoff', lambda r: rules(r).__setitem__('late_cutoff_hours_after_period_end', -1)),
            ('boolean late cutoff', lambda r: rules(r).__setitem__('late_cutoff_hours_after_period_end', True)),
            ('missing late cutoff', lambda r: rules(r).pop('late_cutoff_hours_after_period_end')),
            ('empty rules source', lambda r: rules(r).__setitem__('source', '')),
        ]
        for case, mutate in cases:
            self.rejected(mutate, case, cli=case in ('invented metric', 'days_in_period 1', 'metrics reordered'))

        def contradictory(run):   # the review's second example, all three contradictions at once
            rules(run).update(metrics=['invented'], days_in_period=1, period_end_local_exclusive='1900-01-01')
        self.rejected(contradictory, 'review example 2: contradictory manifest rules', cli=True)

    def test_segment_denominator_end_plan_and_tariff_plan_must_agree_with_the_period(self):
        from rvn_ledger.money import subscription_formula

        def doubled_fee_over_doubled_denominator(run):
            # fee * days / denominator is unchanged, so the arithmetic recomputes; only the denominator rule can see it.
            seg = run.audit['invoices']['acct_c']['subscription_lines'][0]
            seg.update(fee_minor=2 * seg['fee_minor'], days_in_period=60)
            seg['formula'] = subscription_formula(seg['fee_minor'], seg['days'], 60, seg['amount_minor'])

        def end_plan_swapped(run):
            trail = run.audit['invoices']['acct_b']
            trail['period_end_plan_id'] = 'starter'
            for tariff in trail['usage_tariffs'].values():
                tariff['plan_id'] = 'starter'
            for line in trail['usage_lines']:
                line['plan_id'] = 'starter'

        def tariff_plan_swapped(run):
            trail = run.audit['invoices']['acct_b']
            for tariff in trail['usage_tariffs'].values():
                tariff['plan_id'] = 'starter'
            for line in trail['usage_lines']:
                line['plan_id'] = 'starter'

        def overlapping(run):
            seg = run.audit['invoices']['acct_b']['subscription_lines'][0]
            seg.update({'from': '2026-09-02', 'to': '2026-09-18'})          # still 16 days, overlaps the growth segment

        def unordered(run):
            trail = run.audit['invoices']['acct_b']
            trail['subscription_lines'].reverse()
            run.invoices[1]['lines'][:2] = reversed(run.invoices[1]['lines'][:2])

        def empty_end_plan(run):
            trail = run.audit['invoices']['acct_c']
            trail['period_end_plan_id'] = ''
            for tariff in trail['usage_tariffs'].values():
                tariff['plan_id'] = ''

        self.rejected(doubled_fee_over_doubled_denominator, 'segment denominator differs from the manifest', cli=True)
        self.rejected(end_plan_swapped, 'period-end plan is not the plan covering the final day', cli=True)
        self.rejected(tariff_plan_swapped, 'usage priced on another plan than the period-end plan')
        self.rejected(overlapping, 'overlapping segments')
        self.rejected(unordered, 'segments out of chronological order')
        self.rejected(empty_end_plan, 'empty period-end plan')

    def test_final_local_day_must_be_covered_by_the_period_end_plan_segment(self):
        from rvn_ledger.money import subscription_formula

        def uncover(run):
            # A fully consistent 29-day segment (amount, credit and totals recomputed) that stops one day short
            # of the period end: every arithmetic check passes, only the coverage rule can refuse it.
            trail, inv = run.audit['invoices']['acct_c'], run.invoices[2]
            self.assertEqual(inv['account_id'], 'acct_c')
            trail['subscription_lines'][0].update(to='2026-09-30', days=29, amount_minor=26970,
                                                  formula=subscription_formula(27900, 29, 30, 26970))
            inv['lines'][0].update(days=29, amount_minor=26970)
            inv.update(subtotal_minor=26970, credit_applied_minor=26970, credit_remaining_minor=73030, total_minor=0)
            trail['credit'].update(applied_minor=26970, remaining_minor=73030)
            run.manifest['totals_by_currency']['EUR'].update(subtotal_minor=26970, credit_applied_minor=26970, total_minor=0)
        self.rejected(uncover, 'final local day uncovered', cli=True)

    # ---- F2: one event identity, one canonical line ----

    def test_two_canonical_lines_sharing_one_event_identity_are_rejected(self):
        def rename(run, old, new):
            for decision in run.audit['decisions']:
                if decision['event_id'] == old:
                    decision['event_id'] = new
            for entry in run.quarantine:
                if entry['event_id'] == old:
                    entry['event_id'] = new
            for trail in run.audit['invoices'].values():
                for sources in trail['usage_sources'].values():
                    for source in sources:
                        if source['event_id'] == old:
                            source['event_id'] = new

        def two_accepted(run):      # the review's repro: lines 1 and 2 (plus line 2's duplicate) all carry 'a1'
            decisions = run.audit['decisions']
            self.assertEqual([(d['line'], d['event_id'], d['status']) for d in decisions[:3]],
                             [(1, 'a1', 'accepted'), (2, 'a2', 'accepted'), (3, 'a2', 'duplicate_ignored')])
            rename(run, 'a2', 'a1')
        self.rejected(two_accepted, 'review F2: two accepted lines bill one identity', cli=True)
        self.rejected(lambda r: rename(r, 'a4', 'a1'), 'an excluded line shares an accepted identity')
        self.rejected(lambda r: rename(r, 'a7', 'a1'), 'a quarantined winner shares an accepted identity')
        self.rejected(lambda r: rename(r, 'b1', 'd1'), 'two accepted lines of different accounts share one identity', cli=True)

    def test_accepted_identity_must_be_a_nonempty_string(self):
        for bad in ('', None, 7):
            def mutate(run, bad=bad):
                run.audit['decisions'][0]['event_id'] = bad
                run.audit['invoices']['acct_a']['usage_sources']['api_calls'][0]['event_id'] = bad
            self.rejected(mutate, f'accepted identity {bad!r}', cli=bad == '')

    def test_a_line_without_a_readable_sequence_may_share_an_identity_with_a_canonical_line(self):
        # Guard against a false positive: an invalid_ingest_seq line never entered deduplication, so it is
        # quarantined under its own identity and is not a second canonical copy of the accepted event.
        from rvn_ledger.audit import reconcile
        from rvn_ledger.pipeline import run_ledger
        extra = raw_lines([event('a1', '1', 'acct_a', units=3), event('a1', None, 'acct_a', units=3)])
        run = run_ledger({'events.jsonl': RAW_EVENTS + extra, 'accounts.json': json.dumps(ACCOUNTS).encode(),
                          'plans.json': json.dumps(PLANS).encode(), 'period.json': json.dumps(PERIOD).encode()})
        self.assertEqual(run.quarantine[-2:], [{'event_id': 'a1', 'reason': 'invalid_ingest_seq'}] * 2)
        self.assertEqual(run.manifest['counts']['accepted'], 8)
        reconcile(run.invoices, run.quarantine, run.audit, run.manifest)
        self.assertEqual([{**inv, 'quarantined_count': None} for inv in run.invoices], [{**inv, 'quarantined_count': None} for inv in EXPECTED_INVOICES])
        self.assertEqual([inv['quarantined_count'] for inv in run.invoices], [4, 1, 1, 1])


class ManifestTimezoneProvenanceTests(unittest.TestCase):
    """External review (2026-10-02) F3, verification side: the manifest must carry a provenance record for
    exactly the zones the invoices were billed in, and `check` refuses a manifest that does not."""

    def rejected(self, mutate, case):
        from rvn_ledger.audit import AuditError, reconcile
        run = run_fixture()
        mutate(run.manifest)
        with self.subTest(case=case), self.assertRaises(AuditError):
            reconcile(run.invoices, run.quarantine, run.audit, run.manifest)

    def test_provenance_must_name_exactly_the_invoiced_zones_with_valid_records(self):
        def zones(m):
            return m['versions']['timezones']
        self.rejected(lambda m: m['versions'].pop('timezones'), 'missing provenance')
        self.rejected(lambda m: m['versions'].__setitem__('timezones', []), 'provenance is a list')
        self.rejected(lambda m: m['versions'].__setitem__('timezones', {}), 'empty provenance')
        self.rejected(lambda m: zones(m).pop('Europe/Istanbul'), 'invoiced zone missing')
        self.rejected(lambda m: zones(m).__setitem__('Mars/Olympus', dict(zones(m)['UTC'])), 'zone that no invoice uses')
        self.rejected(lambda m: zones(m)['UTC'].__setitem__('sha256', 'abc'), 'short hash')
        self.rejected(lambda m: zones(m)['UTC'].__setitem__('sha256', None), 'null hash on a known source')
        self.rejected(lambda m: zones(m)['UTC'].__setitem__('source', 'guess'), 'unknown source kind')
        self.rejected(lambda m: zones(m)['UTC'].__setitem__('version', 2026), 'version not a string')
        self.rejected(lambda m: zones(m)['UTC'].pop('source'), 'record without source')
        self.rejected(lambda m: zones(m)['UTC'].__setitem__('path', '/usr/share/zoneinfo/UTC'), 'record with an extra field')
        self.rejected(lambda m: zones(m).__setitem__('UTC', None), 'record is null')

    def test_unknown_source_is_an_honest_record_only_without_a_hash(self):
        from rvn_ledger.audit import AuditError, reconcile
        from rvn_ledger.timing import provenance_summary
        run = run_fixture()
        run.manifest['versions']['timezones']['UTC'] = {'source': 'unknown', 'version': '', 'sha256': None}
        run.manifest['versions']['tzdata'] = provenance_summary(run.manifest['versions']['timezones'])
        self.assertTrue(run.manifest['versions']['tzdata'].startswith('mixed: '))
        self.assertTrue(run.manifest['versions']['tzdata'].endswith(', unknown'))
        reconcile(run.invoices, run.quarantine, run.audit, run.manifest)
        with self.assertRaises(AuditError):   # the one-line summary must agree with the records
            reconcile(run.invoices, run.quarantine, run.audit, {**run.manifest, 'versions': {**run.manifest['versions'], 'tzdata': 'system 2026c'}})
        run.manifest['versions']['timezones']['UTC']['sha256'] = 'a' * 64
        with self.assertRaises(AuditError):
            reconcile(run.invoices, run.quarantine, run.audit, run.manifest)
