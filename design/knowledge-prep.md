# Knowledge to prepare — Clearstate cloud migration / data engineering

Mapped against [requirement/jd.md](../requirement/jd.md), grouped by how directly the
JD calls for it. What's specific to the design in this repo is flagged so it's
clear what's general knowledge vs. what needs to trace back to a concrete decision
we made and can defend.

---

## Core (JD: "core requirements")

### Python

- pandas / openpyxl for spreadsheet parsing — specifically the ugly parts: merged
  cells, units baked into headers, footnotes inside data ranges. This is what the
  `_raw` JSON preservation and archive-before-parse pattern in
  [schema-evolution.md](schema-evolution.md) exists to survive.
- Type inference and coercion — see [`schema.py`](../src/clearstate_ingest/schema.py):
  inferring INT64 vs FLOAT64 vs STRING from string samples, handling `#N/A`, `-`,
  thousand-separators, all-null columns defaulting to STRING (the safe top of the
  lattice).
- Writing idempotent, retryable jobs — our `load_id`-keyed delete-then-insert
  pattern in [`runner.py`](../src/clearstate_ingest/runner.py).

### SQL / BigQuery

- Partitioning and clustering — partition by date/range, cluster by high-cardinality
  filter columns, `require_partition_filter = TRUE` so an accidental full scan
  fails instead of billing.
- `MERGE` for incremental loads; `FULL OUTER JOIN` for reconciliation diffs.
- `ALLOW_FIELD_ADDITION` / `ALLOW_FIELD_RELAXATION` — the two schema-update options
  that make additive evolution safe in BigQuery specifically. Worth knowing cold:
  it's *why* widening-only auto-evolution works without a manual ALTER.
- `INFORMATION_SCHEMA.JOBS` for cost attribution per label.
- Window functions — `ROW_NUMBER() OVER (PARTITION BY ... ORDER BY _ingested_at DESC)`
  is literally how our staging view dedupes to the latest row per grain.

### Data modelling

- Star schema basics: `dim_*` / `fct_*`.
- SCD Type 2 — know *when* it's warranted (segment reclassification, vendor
  mergers — anything where "what did this look like historically" matters for a
  year-on-year comparison), not just the mechanics.
- The landing → staging → core → mart layering, and why each layer exists
  (isolation of concerns, not naming convention — see architecture.md §04).

### ETL/ELT

- The distinction itself, and why "load raw, transform in the warehouse" (ELT)
  beats transforming before load, for BigQuery's cost model.
- Idempotency and incremental patterns — merge keys, late-arriving data windows.

---

## Strongly useful (JD: "would also be useful")

### Orchestration

- Cloud Scheduler + Workflows vs. Cloud Composer — know the cost/complexity
  tradeoff cold. We chose Workflows explicitly: Composer's environment cost floor
  isn't justified until cross-DAG backfill semantics are actually needed.
- The concept of a DAG of dependent steps, even without reaching for Airflow.

### CI/CD

- PR-triggered validation (lint, compile, run against a scoped dev dataset) vs.
  merge-triggered promotion — the pattern in architecture.md §10.
- `terraform plan` posted in CI as a review artifact, not just applied blind.

### Cloud Run / Functions

- When each fits: Functions for small event handlers, Cloud Run jobs when you
  need a real container, longer execution, or local-image parity for debugging.
  Our extractor is Cloud Run because it needs pandas/openpyxl and can run long.

### dbt or Dataform

- Not the syntax so much as the concepts: `ref()`, assertions/tests as config,
  incremental models, a compiled DAG from SQL files.
- Know both well enough to justify a choice. We picked Dataform: BigQuery-native,
  no separate runner to host, uses GCP IAM directly.

### Complex spreadsheet-based source data

The most underrated item on this list. Concretely:

- Header rows that aren't row 1, hidden preamble lines, formulas vs. values,
  merged cells breaking positional column reads.
- The mental shift from "parse a file" to "classify what kind of spreadsheet this
  is" — source extract / assumption sheet / calculation engine / deliverable
  output (architecture.md §02). This classification is the single highest-leverage
  idea in the whole design and should be explainable unprompted.

---

## Specific to this design — be fluent, not just aware

These will come up directly if this repo's design is discussed:

- **Schema evolution as a classification problem**, not a binary "did it change"
  check. The AUTO / PROPOSE / BLOCK severity model, and why an *inferred* rename
  must never self-apply — only a *declared* alias is safe to automate.
  ([schema-evolution.md](schema-evolution.md), [`evolution.py`](../src/clearstate_ingest/evolution.py))
- **Reconciliation as a release gate with two tolerance bands** (absolute *and*
  relative — the absolute band is what keeps a 0.1% relative tolerance from being
  meaningless on a value of 3). Match-rate-over-N-consecutive-runs beats a single
  pass/fail check. (architecture.md §07, [`recon.py`](../src/clearstate_ingest/recon.py))
- **The type-widening lattice** — explain in one sentence why INT64→FLOAT64 is
  safe but FLOAT64→INT64 isn't, without hand-waving. (`schema.py`)
- **The `_raw` JSON preservation guarantee** — why a column with no contract home
  yet is still landed, and how that makes later adoption retroactive via
  `COALESCE(canonical, JSON_VALUE(_raw, ...))` in the staging view.

---

## Lower priority for this specific role

- Looker (full platform) — the JD doesn't ask for it; Looker Studio (free tier)
  is what we scoped, with BI Engine as the performance lever, not Looker proper.
- Deep Airflow expertise — the JD says "orchestration" generically, and Composer
  is explicitly the more expensive option we argued against at pilot scale.

---

## If this is interview prep

The top three sections (Python/SQL/modelling, orchestration/CI/CD/Dataform,
spreadsheet classification) are where questions are most likely to land. Ask for
a set of likely questions with model answers grounded in this repo's actual
decisions — that's a stronger use of prep time than a general refresher.
