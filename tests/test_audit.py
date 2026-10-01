import copy
import json
import tempfile
import unittest
from pathlib import Path

from fixtures import EXPECTED_INVOICES, EXPECTED_QUARANTINE, INPUT_NAMES, rewrite_manifest, run_cli, run_fixture, SCRATCH, write_fixture


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
