# Interview prep — Clearstate cloud migration / data engineering

Likely questions mapped to [knowledge-prep.md](knowledge-prep.md), with model answers
grounded in this repo's actual decisions rather than generic textbook answers. Each
answer names the file or section it traces back to, so it can be defended, not just
recited.

---

## Part 1 — Architecture and judgment calls

**Q: Walk me through the target architecture in two minutes.**

> Serverless throughout: Cloud Run extractors pull from Sheets/Excel/Drive, land
> raw into BigQuery's `landing` dataset, Dataform transforms through
> `staging → core → mart`, and Cloud Scheduler + Workflows orchestrates one run:
> extract → load → transform → assert → reconcile → publish → notify. Every
> consumer — Looker Studio, Connected Sheets, the future client portal — reads an
> authorized view over `mart`, never a base table. Two rules do most of the work:
> nothing downstream ever reads `landing` directly, and every step writes an audit
> row to an `ops` dataset. (architecture.md §03)

**Q: Why Dataform over dbt?**

> Dataform is native to BigQuery and serverless — no host to run, uses GCP IAM
> directly, assertions built into the model config. dbt Core has the richer
> ecosystem but needs Cloud Composer or a Cloud Run runner and a second set of
> credentials. For a small team that's added drag with no clear payoff yet. The
> SQL models aren't dbt-specific, so nothing is lost if the team outgrows Dataform
> later. (architecture.md §03, "Component choices")

**Q: Why not Cloud Composer for orchestration?**

> Composer has a fixed environment cost — roughly three figures a month — before
> it runs a single task, plus an Airflow environment to maintain. Cloud Scheduler
> + Workflows costs cents, handles retries and conditional branching, and calls
> Cloud Run and the Dataform API directly. The tradeoff I'd revisit it on is
> cross-dataset dependencies that need a DAG-aware backfill story — Workflows
> doesn't give you that for free.

**Q: How would you decide if this needs BigQuery slot commitments instead of on-demand pricing?**

> Start on-demand. At the data volumes implied here — market intelligence data,
> millions of rows not billions — editions and slot commitments are unlikely to
> pay off before you've actually measured a quarter of usage. The decision is data
> you don't have yet at design time, so I'd instrument cost-per-dataset from
> `INFORMATION_SCHEMA.JOBS` from day one and revisit with real numbers.
> (architecture.md §09)

**Q: What's the biggest risk to this migration succeeding, and it's not technical?**

> Adoption. If the warehouse is harder to use than the spreadsheet it replaces,
> analysts go back to the spreadsheet and the migration fails quietly regardless
> of how correct the pipeline is. That's why Connected Sheets is treated as a
> first-class serving layer, not an afterthought — it gives analysts a live
> BigQuery table inside a spreadsheet with no SQL required, so "fewer manual
> processes" doesn't have to mean "no more spreadsheets." (architecture.md §08)

---

## Part 2 — Data modelling

**Q: Why partition landing tables on ingestion date but core facts on business date?**

> They answer different questions. `landing` partitioning supports "what did we
> load and when" — operational, used for replays and lineage. Core facts are
> partitioned on the business date (e.g. `year`) because that's what the queries
> actually filter on — "give me 2026 data" shouldn't scan every partition ever
> loaded. Using the same partition key for both would optimize the wrong access
> pattern for one of them. (architecture.md §04)

**Q: When do you use SCD Type 2 vs Type 1?**

> Type 2 when the change itself carries information you need for historical
> comparison — segment reclassification, a vendor getting acquired — because a
> year-on-year trend silently breaks if you overwrite the old mapping. Type 1
> everywhere else, because tracking history you'll never query is pure overhead.
> In this design, `dim_product_segment` and `dim_vendor` are Type 2; most other
> dimensions are Type 1.

**Q: Why never update a published fact in place?**

> Because a deliverable that shipped has to stay reproducible. If you mutate a
> row, you can no longer answer "what did this look like when we sent the report
> on March 2nd." Instead, restate: write a new version with a new `load_id`, and
> a view selects the current one. This is also what makes reconciliation
> meaningful — you're diffing against a specific frozen version, not a moving
> target.

**Q: How do `assumption_set_id` and `load_id` work together, and why both?**

> `assumption_set_id` traces a number back to the analyst inputs that produced
> it — which growth rate, which price point. `load_id` traces it back to the
> pipeline run that wrote it. Together, "why did this number change since last
> month" becomes a query instead of an investigation — you can tell apart "the
> analyst changed an assumption" from "the pipeline logic changed."
> (architecture.md §04)

---

## Part 3 — ETL/ELT, orchestration, CI/CD

**Q: Why ELT and not ETL here?**

> BigQuery's cost model rewards pushing transformation into the warehouse — it's
> cheap to scan and cluster, and SQL-based transforms are reviewable in a PR the
> way a Python transform buried in an extractor isn't. The extractors here do the
> minimum: read, validate shape, land raw. Everything interpretive happens in
> Dataform SQL, versioned and tested.

**Q: What makes a pipeline run idempotent, concretely?**

> Every run gets a `load_id`. Loading is delete-then-insert keyed on that id, so
> re-running the exact same `load_id` after a partial failure produces the same
> end state rather than duplicate rows. I found this the hard way, actually — an
> early version of the ops run-log had `load_id` as a primary key with no
> conflict handling, so a genuine retry crashed on a duplicate-key violation
> before it ever got to the real work. Fixed by clearing any prior attempt for
> that `load_id` before re-opening it. Idempotency has to cover your own
> bookkeeping tables, not just the data tables. (`runner.py`, `_open_run`)

**Q: How do you keep prod from running something dev never validated?**

> Promotion is a git tag, not a rebuild. PR merges to main apply Terraform to dev,
> run the full Dataform project, run reconciliation, and publish results to the
> PR. Tagging promotes the *same* artifacts to prod with manual approval and no
> separate build step — so prod runs exactly what dev already validated, not a
> fresh compile that could differ. (architecture.md §10)

---

## Part 4 — Spreadsheet source data (the underrated one)

**Q: What's the first mistake people make migrating spreadsheet-driven processes?**

> Treating "the spreadsheets" as one category. They're not — a purchased-data
> extract, an analyst's assumption sheet, the calculation engine's formula logic,
> and the client-facing deliverable output all need different treatment. Three of
> the four are ordinary engineering work. The calculation engine is analytical
> archaeology — nested lookups and undocumented exceptions someone built up over
> years — and it's the one that needs an analyst in the room, not a developer
> alone. Sizing the whole project on the easy three categories is how estimates
> go wrong. (architecture.md §02)

**Q: Why keep assumptions in Sheets instead of migrating them into BigQuery tables?**

> Because the moment changing a growth rate requires a ticket instead of an edit,
> analysts fork the sheet and the warehouse goes stale within a quarter. The fix
> isn't fighting that instinct — it's making Sheets a governed input: a schema
> contract, validation, and a snapshot into BigQuery on every run. The warehouse
> still becomes system of record; the spreadsheet just stays the interface humans
> touch. (architecture.md §02, "the trap to avoid")

**Q: How do you handle a spreadsheet with a preamble above the header row?**

> `header_row` is a declared field in the source contract, not hardcoded — the
> reader skips `header_row - 1` lines before treating a row as the header. Real
> exports commonly carry a title line and a blank line above the actual columns,
> and that's what this handles without special-casing every source.
> (`extract.py`, `read_csv`)

---

## Part 5 — This repo's specific design (be fluent, not just aware)

**Q: How does the system handle a source that changes shape?**

> Every difference between the contract and the observed schema gets classified
> into one of eleven change types — column added, type widened, rename inferred,
> grain column missing, and so on — and each maps to AUTO, PROPOSE, or BLOCK.
> AUTO evolves the contract and keeps loading. PROPOSE quarantines the rows and
> emits a ready-to-apply contract patch, without touching the contract. BLOCK
> writes nothing anywhere. The run's overall decision is the *most severe*
> classification across all its changes — one blocking change outranks nine safe
> ones. (schema-evolution.md, `evolution.py`)

**Q: Why does an inferred column rename never auto-apply, even at high confidence?**

> Because acting on a wrong guess writes one column's values into another
> column's slot, and that's much worse than a quarantined run. Only a *declared*
> alias — one a human confirmed — is safe to automate. The confirmation is a
> single CLI command (`adopt --column X --alias Y`), and after that the identical
> rename classifies as `column_renamed_declared` and is AUTO from then on. It's a
> one-time cost, not a recurring one. (schema-evolution.md §5)

**Q: What happens to a column that arrives before the contract knows about it?**

> It's still landed — every source column, mapped or not, goes into a `_raw` JSON
> column on the row. So when that column is formally adopted later, the staging
> view can back-fill its history with
> `COALESCE(canonical_col, JSON_VALUE(_raw, '$.the_column'))` instead of showing
> NULL for everything before the adoption date. Nothing is lost by not knowing
> about a column yet. (schema-evolution.md, guarantee G1)

**Q: Explain the type-widening lattice in one sentence.**

> A change from type A to type B is auto-safe exactly when every value
> expressible in A survives being cast to B without loss — INT64 to FLOAT64 is
> safe because every integer is a valid float, but FLOAT64 to INT64 isn't because
> `41200.5` has nowhere to go. (`schema.py`, `is_widening`)

**Q: Why does the reconciliation diff use two tolerance bands instead of one?**

> A single relative tolerance breaks at small numbers — 0.1% of a value of 3 is
> 0.003, which is meaningless noise, not a real check. A single absolute
> tolerance breaks at large numbers, where you actually want to catch a real
> percentage drift. Using the greater of the two — `GREATEST(abs_tolerance,
> baseline * rel_tolerance)` — gives you a meaningful check at both ends of the
> value range. (architecture.md §07, `recon.py`)

**Q: Why gate cutover on three consecutive clean runs instead of one passing run?**

> One clean run could be luck — a day where the specific rows that happen to
> differ didn't get exercised. Three consecutive runs is cheap insurance against
> that, and it forces the reconciliation to survive whatever weekly or monthly
> variation exists in the real data before you retire the legacy calculation.
> (architecture.md §07)

**Q: What's actually designed but not built in this repo, and why does that matter?**

> The GCS archive step — copying each source file byte-for-byte before parsing,
> so a bad parse discovered weeks later can be replayed instead of re-fetched
> from a spreadsheet that's since been edited twelve times. It's specified in
> architecture.md §05 and the lineage hook (`_source_hash`) already exists on
> every landed row, but nothing writes the actual object yet. Flagging that gap
> explicitly — rather than letting the README imply full coverage — is itself
> worth being able to talk about: knowing the boundary of what you've verified is
> part of the job.

---

## Part 6 — Likely curveballs

**Q: What would you do differently with 10x the data volume?**

> Reconsider slot commitments with real cost data, move from full-refresh to
> genuinely incremental Dataform models (the design defaults to full-refresh
> under ~50GB because it's simpler to reason about and premature incrementality
> risks silent data loss), and add a retention policy specifically on the `_raw`
> JSON column, since storing every source column twice stops being negligible at
> that scale. (architecture.md §04 "sizing note", schema-evolution.md §9)

**Q: How would you convince a skeptical analyst to trust the new numbers?**

> Don't ask them to trust it — show them the reconciliation scorecard. Match
> rate, top variances by absolute difference, a total value delta. And be honest
> that not every mismatch means the pipeline is wrong — some variances turn out
> to be errors in the spreadsheet the pipeline replaced, a stale lookup or a
> range that stopped at row 400. The variance report has to be readable by the
> analyst who owns the model, because they're the only one who can tell those two
> cases apart. (architecture.md §07)

**Q: If the interviewer says "we don't have Dataform/dbt experience on the team," how do you respond?**

> The transform layer is SQL either way — Dataform's value-add over hand-rolled
> SQL scripts is version control, `ref()`-based dependency ordering, and
> assertions as config, not a new language to learn. The ramp is closer to
> "learn a thin config wrapper around SQL you already write" than "learn a new
> tool." I'd pair the first few models with whoever owns them so the pattern
> transfers, rather than build it solo and hand over a black box.

---

## How to use this

Read [knowledge-prep.md](knowledge-prep.md) first for the topic map, then this file
for the actual talking points. The "Specific to this design" section (Part 5) is
the one most likely to get follow-up questions if the interviewer has seen this
repo — know it well enough to draw the AUTO/PROPOSE/BLOCK table from memory.
