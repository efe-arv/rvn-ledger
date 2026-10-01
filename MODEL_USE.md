# Model use

Nothing below is implemented: the ledger makes no model calls. The line I'd hold: **a model may read what the ledger produces; nothing a model outputs may change an invoice or an event's outcome.**

**Never on the billing path.** Parsing, deduplication, validation, the period and late checks, proration, tiers, rounding, credits, reconciliation and publishing ([ENGINEERING.md §2](ENGINEERING.md#2-check-precedence)) stay plain code. Each has one correct answer to the cent. A model can't promise byte-identical reruns or an audit trail back to a rule. Event fields are also customer-controlled text, so a model making billing decisions could be steered by instructions hidden in them. The tempting case is the worst: letting a model "repair" a bad record (`"12"` → 12, guessing a timestamp, matching `acct_13` to `acct_013`). That changes money without a rule; the rules say quarantine it.

**Where one helps: the quarantine, at volume.** At 1000× that's about 19,000 records a month.

- *Triage:* code groups records by reason, account and failing field. A typed decision model in the style of Jev (a label from a fixed list plus a probability, never free text) tags each group with a likely cause and owner. Anything low-confidence goes to a person.
- *Statistics:* code computes the counts and month-over-month changes. A language model writes the summary for the owning team, and every number it writes must match the computed table.

Neither one edits or re-admits a record. Fixes happen upstream, corrected events go through the same run, and billing never waits on either.

**Local models only.** Quarantined records are customer usage data, so they never go to an inference provider. Both steps run on open-weight models inside our own environment. For triage that means a self-hosted Jev-style model such as [Kev](https://github.com/jaredpalmer/kev), retrained and calibrated on our own human-labelled quarantine history. Weights are pinned by hash and recorded with every label, just as the manifest records code and tzdata.

**Or none at all.** At today's 19 quarantined records, `rvn-ledger explain` and a person are enough, so a no-model answer is just as defensible. Volume decides whether the advisory side is worth it; the line doesn't move.
