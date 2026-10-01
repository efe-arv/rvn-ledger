# Engineering decisions

## 1. Scope and design

This ledger converts four local input files into exact, reproducible invoices, a quarantine report, and an auditable run manifest. The billing specification and the output contract in `period.json` are authoritative. The implementation does not infer pricing policy, repair records silently, or approximate monetary results.

The core is a deterministic batch pipeline:

```text
Reference-data validation
    → strict event parsing and canonical-copy selection
    → event validation and account-local time filtering
    → usage aggregation and subscription segmentation
    → cumulative tier pricing and line-level rounding
    → capped credit application
    → audit reconciliation and staged publication
```

The stages are separated into small Python modules so that money, time boundaries, record selection, and publication can be tested independently. The billing path makes no network or large language model (LLM) calls. Authentication, a database, a user interface, and hosted deployment are unnecessary for the assignment.

The packaged CLI adds installation, diagnostics, and Excel transport around this core. Those extensions do not define billing policy: the JSON inputs and validated JSON output set remain authoritative.

## 2. Invariants

### Exact money, with rounding at the specified boundary

Monetary amounts are nonnegative Python integers in minor currency units. Booleans are rejected even though Python treats them as integers. Floating-point values are not accepted as amounts, and output serialization rejects floats anywhere in the documents.

For a nonnegative integer numerator `n` and a positive integer denominator `d`, half-up rounding is computed without floating-point division:

```text
round_half_up(n, d) = (2 × n + d) // (2 × d)

subscription line = round_half_up(fee_minor × local_days, days_in_period)
usage line        = round_half_up(units × unit_price_micros, 10000)
subtotal_minor    = sum(already-rounded line amounts)
credit_applied    = min(subtotal_minor, credit_minor)
total_minor       = subtotal_minor − credit_applied
credit_remaining  = credit_minor − credit_applied
```

Rounding the combined unrounded charges would implement a different pricing rule. Two half-minor-unit lines must each round up; their subtotal must not be obtained by rounding their combined value once. Credits cannot make the invoice negative, and increasing a credit cannot increase the payable total.

Currency is part of the pricing lookup, not a display label. Fees and tariffs are selected in the account's currency; missing required currency or metric pricing fails the run. Cross-account summaries remain separated by currency. There is no exchange-rate conversion.

### Every source line has exactly one disposition

Every physical event line, including an unreadable or malformed line, receives exactly one terminal status:

- `accepted`
- `duplicate_ignored`
- `excluded_out_of_period`
- `excluded_late`
- `quarantined`

Their counts sum to the raw line count. Assigning two statuses to a line, or leaving a line unclassified, is an internal error rather than a silent overwrite.

For a repeated `event_id`, the smallest `ingest_seq` selects the canonical copy. Business validity does not influence that selection. A later valid copy cannot repair an invalid canonical copy. Rejected JSON can participate in deduplication only when its top-level identity and sequence can be recovered unambiguously; recovery does not make the payload valid.

Every accepted event contributes to exactly one account-and-metric usage aggregate. Its units are conserved through aggregation and cumulative tier allocation. Duplicate, excluded, and quarantined records contribute no billable units.

### Local dates and elapsed time are different concepts

Each account's billing interval is half-open: `[local_start, local_end)`. Local-midnight boundaries are resolved with the account's IANA time zone and then compared as UTC instants. A timestamp exactly at the start is eligible; one exactly at the end is outside the period.

The lateness cutoff is the account-local period end converted to UTC, plus 48 elapsed hours. Arrival exactly at the cutoff is allowed; arrival after it is excluded. Timestamp fractions are retained as exact rational seconds for boundary comparisons, rather than truncated to `datetime`'s microsecond resolution.

Subscription proration counts whole local calendar days in each clipped plan segment, not elapsed hours divided by 24. Daylight-saving transitions therefore do not change the number of billable calendar days. Usage is priced using the plan covering the final local day of the period; tier consumption is cumulative across the entire period, not reset on plan changes or per event.

An account with no accepted usage still receives its subscription invoice. Host-dependent time-zone aliases are rejected rather than allowing the server's configuration to determine an account's billing interval.

### Reproducibility includes the execution environment

Output ordering is explicit: invoices by account identifier; subscription lines chronologically; usage lines by the configured metric order and tier order; quarantine entries by source line. Serialization is stable and contains no wall-clock timestamp, random identifier, hostname, or absolute source path.

Identical input bytes under the same implementation and recorded runtime/time-zone environment produce byte-identical output. This qualification matters: the manifest records code hashes, Python version, and the resolved time-zone database version. Different code or time-zone data must not be presented as the same reproducibility environment.

### Auditability is stronger than an unexplained total

`audit.json` records source-line decisions, duplicate targets, accepted-event references, subscription segments, and the pricing inputs needed to reconstruct invoice lines. The manifest records input hashes and sizes, output hashes and sizes, disposition counts, currency-separated totals, and implementation/runtime versions.

Internal reconciliation checks arithmetic, unit conservation, tier allocation, source-use uniqueness, and consistency between the invoices and their audit trail. It is not independent authentication of every raw input or tariff. The separate invoice verifier recomputes the supplied-data invoices from raw inputs.

Hashes detect changes relative to an unchanged manifest. They are not signatures: an attacker able to replace the entire output set and its manifest can create a self-consistent replacement.

## 3. Check precedence

Precedence is billing policy. Reordering checks can change which copy wins, which reason is reported, or whether a record is billable.

### Before events: validate reference data

The run validates account identifiers and time zones, period dates, metric definitions, plan segments, required fees and tariffs, and credits before classifying events. Required pricing is checked even for accounts or metrics with no usage.

Invalid reference data stops the run. It cannot be handled as an isolated quarantined event because it makes the interpretation of otherwise valid events unreliable.

### For each event: select first, then validate, then filter

1. **Parse strictly.** Reject invalid UTF-8, invalid JSON, non-object values, repeated keys, non-finite numbers, and unsupported parser-depth or integer-size cases. Preserve a stable parse reason and recover ordering identity only when unambiguous.
2. **Establish identity.** Require a nonempty string `event_id` and a non-boolean integer `ingest_seq`. A line without usable ordering identity cannot claim an event identifier.
3. **Select the canonical copy.** Choose the minimum sequence before business validation. Later copies become `duplicate_ignored`, even when their payload would otherwise pass validation.
4. **Validate the winner.** Collect failures in fixed order: account, metric, units, usage timestamp, ingestion timestamp. The first reason is written to the required quarantine report; the complete ordered list is retained in the audit trail.
5. **Check the billing period.** A valid winner outside the account-local interval becomes `excluded_out_of_period`.
6. **Check lateness.** An otherwise eligible winner arriving after the cutoff becomes `excluded_late`.
7. **Accept and aggregate.** Only records that pass all preceding steps enter billing.

A parse failure with recoverable identity may be a canonical quarantined copy or an ignored later duplicate. It is never accepted merely because identity recovery succeeded. A winner with invalid fields is quarantined before time exclusion; a valid winner that is both out of period and late is classified as out of period.

For each account, calculation then proceeds through subscription segments, period-end-plan usage tiers, per-line rounding, subtotal, capped credit, and final total.

## 4. Explicit assumptions and failure boundaries

- Equal `ingest_seq` values use physical source-line order as the tie-breaker. This resolves an unspecified case deterministically for a fixed input file; it is not claimed as a rule supplied by the assignment. Reordering tied copies can change the winner.
- Empty usage brackets emit no usage line. Zero-usage accounts still receive subscription invoices.
- An account's `quarantined_count` includes only quarantined records naming that known account. Unknown-account and unreadable records remain in the global quarantine report.
- Plan-segment gaps inside the period incur no subscription fee, but a segment must cover the final local day so period-end usage pricing is defined. Overlapping or unusable configuration is rejected.
- Event quarantine means record, explain, and continue. It does not authorize inferred timestamps, unit coercion, a replacement canonical copy, or automatic correction.
- Publication uses an exclusive staging directory, staged writes, and manifest-last replacement. Caught replacement errors trigger rollback. This is not a crash-atomic transaction across four files. Consumers must wait for the writer to finish and validate the complete output set; interrupted publication or failed rollback requires recovery, not blind consumption.
- Excel reports are derived review artifacts. Input workbooks use an explicit lossless schema, not heuristic spreadsheet interpretation. They do not relax event validation or monetary precision requirements.

## 5. Verification strategy

The test suite covers the specified hazards individually and combines them in regression cases. Seeded property tests check conservation and independence properties, including credit monotonicity, duplicate non-interference, and account isolation. File-order invariance applies where canonical-copy ordering is unambiguous; it does not override the documented equal-sequence tie-breaker.

Correctness checks use explicit runtime validation, not Python `assert` statements that disappear under optimization. The suite is also exercised with optimized Python execution. Repeated runs and differing host time-zone settings check that ambient host settings do not change the output for a fixed billing environment.

Independent raw-input recomputation complements internal reconciliation. Regression tests specifically cover hazards found during review, including sub-microsecond cutoff violations and malformed Unicode identifiers that previously threatened publication. A correct result on the supplied data is not sufficient evidence that those boundary cases are correct.

The assignment's hidden reference invoices are unavailable. Local verification demonstrates the implemented rules and observed cases; it does not claim that the evaluator's private comparison has been run.

## 6. What changes at 1000× volume

The current implementation materializes inputs, parsed records, classifications, source references, and serialized output in memory. This is a deliberate simplicity trade-off for the supplied batch, not a claim of bounded-memory streaming.

At 1000× volume, I would first measure peak memory, parsing time, canonical-copy selection, audit construction, serialization, and output I/O. The likely first constraint is retained per-event state, especially the audit trail. The billing rules, precedence, integer arithmetic, and traceability requirements would remain unchanged.

### Bound memory without changing canonical-copy semantics

Use an immutable, hashed input snapshot and two passes over it. The first pass selects the minimum `(ingest_seq, source_line)` for each globally unique event identifier, applying the same strict parsing and identity-recovery rules. The second pass classifies against those winners and folds accepted units into account-and-metric totals.

This requires `O(unique event identifiers)` selection state plus account/metric aggregates. It is not `O(accounts × metrics)` overall. If the winner index does not fit comfortably in memory, use external sorting or a disk-backed temporary index. Keep original source-line references and verify both passes read the same snapshot.

### Stream the audit, not just the amounts

Write disposition records and accepted-source references to deterministic audit shards rather than retaining every event object. Keep aggregate pricing state in memory, with stable offsets or content hashes linking invoice lines to their source records. An account's accepted events are not necessarily contiguous in the original input, so a single source-file range is insufficient unless references are explicitly grouped during spooling.

Full traceability still has linear storage cost. Streaming reduces the working set; it does not eliminate the evidence.

### Partition after global deduplication

Partitioning raw events by account would be wrong: conflicting copies of the same event identifier may name different accounts. Select the global canonical copy first, or partition the selection phase by event identifier. Then route accepted winners to account shards for independent billing.

Merge invoices by account identifier and restore source-line order for the required quarantine output. Worker completion order must not affect serialization. Shard manifests should bind inputs, code, time-zone data, and output hashes so resumed work cannot mix execution environments.

### Publish complete generations

If crash-safe resumability becomes necessary, replace the flat-file publication protocol with immutable run-generation directories and a small atomic completion pointer, using appropriate filesystem durability handling. Publish a generation only after every shard and the final merged outputs reconcile. Readers must never combine files from different generations.

A million-event monthly batch does not, by itself, justify a service, message queue, or permanent database. I would add those only for demonstrated requirements such as concurrent ingestion, incremental billing, or operational recovery. The first scaling step is a measured, disk-assisted batch implementation, not a distributed rewrite.
