# Schema evolution

**Design document — how the pipeline absorbs a source that changes shape**

| | |
|---|---|
| **Status** | Implemented in [`src/clearstate_ingest/`](../src/clearstate_ingest/), 180 tests passing |
| **Applies to** | Every spreadsheet, Excel and Sheets source in the programme |
| **Companion** | [architecture.md](architecture.md) §05 (ingestion), §06 (orchestration) |
| **Diagram** | [diagrams/schema-evolution.mmd](diagrams/schema-evolution.mmd) |

---

## 1. The problem

Spreadsheet sources change shape constantly, and almost never maliciously. An analyst inserts a column, renames a header for a client presentation, types a decimal into an integer column, or splits a tab in two. In a conventional pipeline each of those is an outage: the load fails, someone is paged, and the fix is a code change and a deploy.

That failure mode is fatal here specifically, not just annoying. §01 of the architecture argues that the migration succeeds or fails on whether analysts trust and use the warehouse. A pipeline that breaks every time someone edits their own spreadsheet trains people to route around it — and the spreadsheet becomes the source of truth again.

So the requirement is not "detect schema drift". It is: **the system continues to work when the source changes, escalating only what genuinely needs a human.**

The opposite failure is just as bad. A pipeline that silently absorbs anything will happily map a renamed column to the wrong target, coerce a price into an integer, or drop a column nobody noticed was gone — and the numbers will be wrong in a way reconciliation catches months later, if at all.

The design is therefore a classification problem: for each individual difference between what the contract expects and what arrived, decide which of three things to do.

---

## 2. Three guarantees

Everything below follows from these. They are the properties worth defending in review.

### G1 — Nothing is ever silently dropped

Every landing table carries a `_raw` JSON column holding **every** source column, mapped or not. A column that appears before anyone has adopted it is still landed, from the first row of the first file.

The payoff is retroactive. When `confidence_code` is adopted at contract v2, the historical partitions written under v1 are not NULL — the staging view recovers the value from `_raw`:

```sql
COALESCE(
  confidence_code,
  SAFE_CAST(JSON_VALUE(_raw, '$.confidence_code') AS STRING)
) AS confidence_code
```

Without this, adopting a column means either losing its history or re-ingesting archived files. With it, adoption is a contract edit.

### G2 — Canonical names never change

A source-side rename is absorbed as an **alias on the existing column**, never by renaming the warehouse column. `core` models, marts, dashboards and deliverables never see it.

This is why renames are cheap here and expensive elsewhere. The alternative — renaming the column downstream — turns one analyst's header edit into a migration across every model that referenced it.

### G3 — Ambiguity is escalated, never guessed

The engine can *infer* that `average_selling_price_usd` is a rename of `asp_usd`. It will not act on that inference. It quarantines the load, emits the exact contract patch that would make it true, and waits for a person to confirm. Once confirmed, the same change is automatic forever after.

---

## 3. Classification

Every difference is assigned exactly one change class, and each class maps to an action. The map is data ([`policy.py`](../src/clearstate_ingest/policy.py)), so a contract can tighten or loosen any single class without touching the engine.

| Change class | Default | Why |
|---|---|---|
| `column_added` | **AUTO** | Purely additive. Nothing downstream can break; the value is captured either way (G1). |
| `column_reordered` | **AUTO** | Not a change at all — every mapping in the system is by name. Reported for the log only. |
| `type_widened` | **AUTO** | Lossless by construction. See the lattice in §4. |
| `column_renamed_declared` | **AUTO** | A human already confirmed this alias. That is what declaring it means. |
| `column_renamed_inferred` | PROPOSE | Plausible, unproven. Acting on it risks writing one column's values into another. |
| `type_narrowed` | PROPOSE | May fit, may truncate. Only the data can say, and only a person can accept the risk. |
| `column_removed_optional` | PROPOSE | Loading NULL is *probably* right, but a silently emptied column is a classic way for a deliverable to go quietly wrong. |
| `required_relaxed` | PROPOSE | A required column present but empty in every row — the source has stopped populating it. |
| `column_removed_required` | **BLOCK** | The contract says the model cannot be built without it. |
| `type_incompatible` | **BLOCK** | No safe cast exists in either direction. |
| `grain_column_missing` | **BLOCK** | Without the grain there is no identity, no dedupe, and no reconciliation. |

**The run's decision is the most severe action across all its changes.** A file with nine safe additions and one missing grain column is blocked. Severity does not average out.

### What each action does

| Action | Contract | Landing table | Downstream sees | Who is told |
|---|---|---|---|---|
| **AUTO** | version bumped, columns added, types widened | rows loaded normally | new data | run summary only |
| **PROPOSE** | unchanged | untouched — rows go to `landing_quarantine_*` with a reason | the last good partition | contract owner, with a patch |
| **BLOCK** | unchanged | nothing written, anywhere | the last good partition | contract owner, by name, as a failure |

The PROPOSE row is the important one. A quarantined run is not a failure: yesterday's correct numbers stay served, today's rows are preserved and inspectable, and the owner gets a specific edit to approve rather than a stack trace.

---

## 4. The type lattice

Widening is defined as a directed graph, and safety is reachability in it. This is the whole of the type reasoning — there are no special cases elsewhere in the codebase.

```
BOOL ──► INT64 ──┬─► NUMERIC ──┐
                 └─► FLOAT64 ◄─┘
                        │
DATE ──► DATETIME ──► TIMESTAMP
                        │
                        ▼
                     STRING          (top — everything widens into it)
```

- `is_widening(a, b)` — `b` is reachable from `a`. Lossless, so **AUTO**.
- `is_narrowing(a, b)` — `a` is reachable from `b`. Possibly lossy, so **PROPOSE**.
- Neither — no safe cast in either direction, so **BLOCK**. `DATE` against `INT64` is the canonical case: they meet only at `STRING`.

Two deliberate choices:

**An all-null column infers as `STRING`.** `STRING` widens from nothing, so the first real value that arrives registers as a *narrowing* and gets surfaced. The alternative — guessing a type from no evidence — silently coerces the first real data.

**Inference takes the narrowest type that holds every sampled value.** `["1", "2"]` is `INT64`; `["1", "2.5"]` is `FLOAT64`; `["1", "SG"]` collapses to `STRING`.

---

## 5. Rename inference

Renames are the interesting case, because a rename is indistinguishable from *one column removed and another added* unless you look at names and types together.

The engine pairs every unmatched observed column with every unmatched contract column and scores them:

```python
score = max(
    SequenceMatcher(a, b).ratio(),          # catches country_code -> countrycode
    len(tokens_a & tokens_b) / min(len(a), len(b)),   # catches asp_usd -> average_selling_price_usd
)
```

Neither metric alone is enough — sequence similarity misses the token case, token overlap misses the concatenation case. Pairs scoring ≥ 0.5 **and** having compatible types become candidates; the highest-scoring pairs are matched greedily, one-to-one.

The threshold is deliberately generous because a false positive is cheap here: it becomes a PROPOSE with a patch a human rejects. A false *negative* is expensive — it degrades into `column_removed_required`, which blocks. Tuned in the direction where being wrong costs least.

### The confirmation loop

```
run 1   average_selling_price_usd appears
        -> column_renamed_inferred (similarity 0.50)
        -> PROPOSE: rows quarantined, patch emitted

        $ clearstate-ingest adopt sources/market_inputs.yaml \
              --column asp_usd --alias average_selling_price_usd

run 2   same header
        -> column_renamed_declared
        -> AUTO: rows load normally, forever after
```

One reviewed command turns a recurring incident into a permanent non-event.

---

## 6. The registry

Every distinct schema shape a source has ever produced is fingerprinted and recorded in `ops.schema_registry`:

```
fingerprint = sha256(sorted("name:type:required" for each column))[:16]
```

Sorted, so **column order does not affect the fingerprint** — reordering is not a schema change, consistent with mapping by name throughout.

The registry does two jobs. It short-circuits the common case: a fingerprint already seen needs no diff. And it gives the schema an audit history — `ops.schema_evolution_log` records every change, its classification, its decision, its confidence score and the contract versions it moved between. "When did this column appear, and who approved it?" is a query.

---

## 7. Worked example

The four snapshots in [`sample_data/`](../sample_data/) exist to exercise exactly this, and the assertions below are the ones in [`tests/test_pipeline.py`](../tests/test_pipeline.py).

| Snapshot | What the analyst did | Classified as | Decision | Outcome |
|---|---|---|---|---|
| **v1** 2026-01-05 | nothing | — | AUTO | 18 rows loaded, contract stays v1 |
| **v2** 2026-02-02 | added `confidence_code`, moved it to column A | `column_added` + `column_reordered` | AUTO | 18 rows loaded, contract → v2, column added to the table, value also in `_raw` |
| **v3** 2026-03-02 | renamed `asp_usd`, typed one volume as `41200.5` | `column_renamed_inferred` + `type_widened` + `column_added` | **PROPOSE** | 0 rows to landing; quarantined with a reason; patch names the alias |
| **v4** 2026-04-06 | moved `segment_id` to its own tab | `column_removed_required` + `grain_column_missing` | **BLOCK** | nothing written anywhere; owner alerted |

Reproduce end to end:

```bash
python -m clearstate_ingest.cli plan sources/market_inputs.yaml \
    --file sample_data/market_inputs_v3_2026-03-02.csv
# exit 1 (PROPOSE), with the contract patch printed
```

Note the second-order effect in v3: because PROPOSE does **not** evolve the contract, `volume_units` is still `INT64` when validation runs, so the row containing `41200.5` fails coercion and is quarantined separately. Correct behaviour — the widening was safe *in principle*, but the run was held for an unrelated reason, and the pipeline does not half-apply a plan.

---

## 8. In BigQuery

The local SQLite backend and the production BigQuery backend implement one interface, so the pipeline code is identical. Only the mechanics differ:

| Operation | BigQuery mechanism |
|---|---|
| Add a column | `ALLOW_FIELD_ADDITION` on the load job, or `update_table(schema)` |
| Relax REQUIRED → NULLABLE | `ALLOW_FIELD_RELAXATION` |
| Widen a type | `CREATE OR REPLACE TABLE … AS SELECT CAST(col AS <wider>)` — BigQuery has no in-place widen. Safe only because the engine has already proved the cast lossless. |
| Preserve unmapped columns | native `JSON` column, read with `JSON_VALUE` |
| Insulate downstream | the staging view in §2, regenerated whenever the contract changes |

`clearstate-ingest sql <contract> --what staging_view` emits the current view; it is a pure function of the contract, so it belongs in the Dataform project and is reviewed in a pull request like any other model.

---

## 9. Limits, and what I would do next

Stated plainly, because these are the questions a reviewer should ask.

- **Rename inference is name-and-type based, not value-based.** Comparing value distributions between the removed and added column would raise confidence considerably, especially for renames with no shared tokens. Not built — the confirmation loop makes a miss cheap.
- **One-to-one renames only.** A column split into two, or two merged into one, classifies as a removal plus additions and lands in PROPOSE or BLOCK. That is the right *outcome*, but the message could name the likely split.
- **`_raw` costs storage.** Every source column is stored twice. At market-intelligence volumes this is negligible; at hundreds of millions of rows it would want a retention policy on `_raw` specifically — say, drop it from partitions older than a year once the contract has stabilised.
- **The policy is per-contract, not per-column.** Tightening `type_narrowed` to BLOCK applies to every column in that source. Per-column overrides are a small change to `Policy` if a source turns out to need them.
- **AUTO adoption of a new column does not infer its semantics.** It arrives typed and nullable with no description and no assertions. The dossier step (§11 of the architecture) is where a human gives it meaning; nothing in the pipeline enforces that they do.

---

## 10. Where the code lives

| Concern | Module |
|---|---|
| Types, widening lattice, inference, fingerprinting | [`schema.py`](../src/clearstate_ingest/schema.py) |
| Change classes and the policy map | [`policy.py`](../src/clearstate_ingest/policy.py) |
| Classification, rename inference, plan application | [`evolution.py`](../src/clearstate_ingest/evolution.py) |
| Contract loading, validation, round-trip | [`contracts.py`](../src/clearstate_ingest/contracts.py) |
| Canonicalisation into `_raw`, value validation | [`validate.py`](../src/clearstate_ingest/validate.py) |
| The seven-step run and ops logging | [`runner.py`](../src/clearstate_ingest/runner.py) |
| BigQuery DDL, staging view, recon SQL | [`sqlgen.py`](../src/clearstate_ingest/sqlgen.py) |
| SQLite and BigQuery adapters | [`warehouse.py`](../src/clearstate_ingest/warehouse.py) |
| Tests for all of the above | [`tests/`](../tests/) |
