# Clearstate — spreadsheets to BigQuery

Target architecture, a working reference implementation, and sample data for the
Clearstate cloud migration brief in [requirement/jd.md](requirement/jd.md).

The design argument in one line: **the warehouse becomes the system of record, the
spreadsheet stays as an interface, and nothing is called "migrated" until it
reconciles against the deliverable it replaces.**

---

## Layout

```
requirement/jd.md              the brief
design/
  architecture.md              target architecture, 12 sections
  architecture.html            the same, as a styled standalone page
  schema-evolution.md          design doc for handling sources that change shape
  diagrams/*.mmd               Mermaid sources (architecture, evolution, sequence, recon)
src/clearstate_ingest/         the reference implementation
sources/market_inputs.yaml     an example source contract
sample_data/                   four schema snapshots + a reconciliation baseline
tests/                         180 tests, no cloud account needed
```

## Quick start

```bash
python -m pip install -e ".[dev]"     # or just: set PYTHONPATH=src
python sample_data/generate.py        # regenerate sample data (deterministic)
python -m pytest                      # 180 passing
```

Nothing here needs a GCP project. The pipeline runs against a local SQLite
warehouse through the same code path production uses against BigQuery.

## Try the thing it is actually for

Four snapshots of one source sheet, each a different schema event:

```bash
export PYTHONPATH=src

# v1 - the shape the contract was written against
python -m clearstate_ingest.cli run sources/market_inputs.yaml \
    --file sample_data/market_inputs_v1_2026-01-05.csv --db wh.db
# SUCCESS - 18 in, 18 loaded, no schema change

# v2 - analyst adds a column and moves it to the front
python -m clearstate_ingest.cli run sources/market_inputs.yaml \
    --file sample_data/market_inputs_v2_2026-02-02.csv --db wh.db
# SUCCESS - contract auto-evolved v1 -> v2, column adopted, reorder ignored

# v3 - a column is renamed and a volume arrives as a decimal
python -m clearstate_ingest.cli run sources/market_inputs.yaml \
    --file sample_data/market_inputs_v3_2026-03-02.csv --db wh.db
# QUARANTINED - the rename is only inferred, so it is proposed, not applied.
#               Landing is untouched; a contract patch is printed.

# confirm the rename once, and it is automatic forever after
python -m clearstate_ingest.cli adopt sources/market_inputs.yaml \
    --column asp_usd --alias average_selling_price_usd

# v4 - someone moves segment_id to its own tab
python -m clearstate_ingest.cli run sources/market_inputs.yaml \
    --file sample_data/market_inputs_v4_2026-04-06_breaking.csv --db wh.db
# BLOCKED - a grain column is gone. Nothing is written anywhere.
```

Reconcile a candidate against the frozen legacy deliverable:

```bash
python -m clearstate_ingest.cli recon sources/market_inputs.yaml \
    --baseline  sample_data/legacy_market_size_baseline.csv \
    --candidate sample_data/pipeline_market_size_candidate.csv
```

```
market_inputs: match rate 84.21% of 19 comparisons - FAIL
  extra_in_new=1, match=16, missing_in_new=1, variance=1
  value delta: +818,697.37
```

Emit the BigQuery SQL for a contract:

```bash
python -m clearstate_ingest.cli sql sources/market_inputs.yaml --what landing_ddl
python -m clearstate_ingest.cli sql sources/market_inputs.yaml --what staging_view
python -m clearstate_ingest.cli sql sources/market_inputs.yaml --what recon
python -m clearstate_ingest.cli sql sources/market_inputs.yaml --what dataform
```

## Exit codes

The CLI is built to be gated on in CI:

| Code | Meaning |
|---|---|
| `0` | safe — loaded, or the plan is entirely auto-evolvable |
| `1` | needs review — quarantined, or reconciliation below threshold |
| `2` | blocked — a breaking schema change; nothing was written |
| `3` | error |

## Onboarding a second source

The point of the exercise (architecture.md §11): it should be a YAML file, not a
Python module.

```yaml
source_id: vendor_shipments
kind: csv
path: sample_data/vendor_shipments.csv
header_row: 1
grain: [country_code, vendor_id, year]
owner: analytics-team@clearstate.example
on_violation: quarantine
recon:
  measures: [units]
  rel_tolerance: 0.001
columns:
  - {name: country_code, type: STRING,  required: true, regex: "^[A-Z]{2}$"}
  - {name: vendor_id,    type: STRING,  required: true}
  - {name: year,         type: INT64,   required: true, min: 2015, max: 2040}
  - {name: units,        type: INT64,   required: true, min: 0}
```

Then `clearstate-ingest run sources/vendor_shipments.yaml`. No code.

## What is real and what is illustrative

**Real and tested** — the schema evolution engine, contracts, validation, the
reconciliation diff, SQL generation, the run orchestration, ops logging, and the
SQLite warehouse. 180 tests cover them.

**Written but not exercised here** — the BigQuery and Google Sheets adapters.
They are the production path and need a GCP project and credentials; the code is
in `warehouse.py` and `extract.py` behind lazy imports so everything else runs
without them.

**Designed but NOT implemented** — the GCS archive step. `architecture.md` §05
specifies that Cloud Run copies every source file byte-for-byte to
`gs://cs-archive/...` before parsing, and that archive is what makes a wrong parse
recoverable six weeks later. No code in `src/` does this yet. The lineage hook
exists — every landed row carries `_source_hash`, the content hash that would be
the object key — but nothing writes the object. Closing this needs an `Archive`
interface alongside `Warehouse` (a local implementation for tests, a GCS one for
production) wired into step 2 of the run.

**Illustrative** — the market-sizing data shape, the volumes, and the 99.5% / 0.1%
reconciliation tolerances. All of it is pending the six open questions in
architecture.md §12, the first being which dataset is actually the pilot.

## Reading order

1. [design/architecture.md](design/architecture.md) — what is being built and why
2. [design/schema-evolution.md](design/schema-evolution.md) — the hardest part, in detail
3. [design/diagrams/](design/diagrams/) — the same, as pictures
4. `tests/test_pipeline.py` — the design claims, as executable assertions
