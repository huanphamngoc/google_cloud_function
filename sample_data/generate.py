"""Generate the sample dataset.

Four snapshots of the same source sheet, each one a schema event the pipeline has
to handle, plus a legacy deliverable to reconcile against. Deterministic - no
randomness - so test assertions can be exact.

    python sample_data/generate.py

  v1  2026-01-05  baseline shape                     -> AUTO    (no change)
  v2  2026-02-02  new column, columns reordered      -> AUTO    (adopt + ignore)
  v3  2026-03-02  asp_usd renamed, volume gets a
                  decimal value                      -> PROPOSE (inferred rename)
  v4  2026-04-06  segment_id dropped entirely        -> BLOCK   (grain destroyed)
"""

from __future__ import annotations

import csv
import io
import os

HERE = os.path.dirname(os.path.abspath(__file__))

COUNTRIES = ["SG", "MY", "TH"]
SEGMENTS = ["IVD-CHEM", "IVD-IMMUNO", "IVD-MOLEC"]
YEARS = [2025, 2026]

# deterministic, vaguely plausible inputs
BASE_VOLUME = {
    ("SG", "IVD-CHEM"): 41200,
    ("SG", "IVD-IMMUNO"): 28650,
    ("SG", "IVD-MOLEC"): 12400,
    ("MY", "IVD-CHEM"): 96500,
    ("MY", "IVD-IMMUNO"): 61300,
    ("MY", "IVD-MOLEC"): 24800,
    ("TH", "IVD-CHEM"): 128400,
    ("TH", "IVD-IMMUNO"): 79200,
    ("TH", "IVD-MOLEC"): 31500,
}
ASP = {"IVD-CHEM": 18.40, "IVD-IMMUNO": 32.75, "IVD-MOLEC": 96.20}
GROWTH = {"IVD-CHEM": 4.20, "IVD-IMMUNO": 6.80, "IVD-MOLEC": 11.50}
NOTES = {
    ("SG", "IVD-MOLEC"): "public tender pending, volumes may shift",
    ("TH", "IVD-CHEM"): "distributor restated 2025 shipments",
}

PREAMBLE = [
    ["Clearstate - APAC IVD market inputs"],
    ["Exported {date} - do not edit below the header row"],
]


def records():
    """The canonical rows every snapshot is built from."""
    for country in COUNTRIES:
        for segment in SEGMENTS:
            for year in YEARS:
                vol = BASE_VOLUME[(country, segment)]
                if year == 2026:
                    vol = int(round(vol * (1 + GROWTH[segment] / 100.0)))
                asp = ASP[segment] * (1.03 if year == 2026 else 1.0)
                yield {
                    "country_code": country,
                    "segment_id": segment,
                    "year": year,
                    "volume_units": vol,
                    "asp_usd": round(asp, 2),
                    "value_usd": round(vol * round(asp, 2), 2),
                    "growth_pct": GROWTH[segment] if year == 2026 else 0.00,
                    "analyst_note": NOTES.get((country, segment), ""),
                }


def write(path, header, rows, date, preamble=True):
    full = os.path.join(HERE, path)
    with io.open(full, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        if preamble:
            for line in PREAMBLE:
                w.writerow([c.format(date=date) for c in line])
        w.writerow(header)
        for row in rows:
            w.writerow(row)
    print("wrote {} ({} rows)".format(path, len(rows)))


def main() -> None:
    data = list(records())

    # ---- v1: the shape the contract was written against ----
    header = [
        "country_code",
        "segment_id",
        "year",
        "volume_units",
        "asp_usd",
        "value_usd",
        "growth_pct",
        "analyst_note",
    ]
    write(
        "market_inputs_v1_2026-01-05.csv",
        header,
        [[r[c] for c in header] for r in data],
        "2026-01-05",
    )

    # ---- v2: analyst adds a confidence column and moves it to the front ----
    # AUTO: purely additive, and reordering is irrelevant because we map by name.
    conf = {"IVD-CHEM": "H", "IVD-IMMUNO": "M", "IVD-MOLEC": "L"}
    header_v2 = ["confidence_code"] + header
    rows_v2 = [[conf[r["segment_id"]]] + [r[c] for c in header] for r in data]
    write("market_inputs_v2_2026-02-02.csv", header_v2, rows_v2, "2026-02-02")

    # ---- v3: asp_usd renamed, and one volume arrives as a decimal ----
    # PROPOSE: the rename is only inferred. The INT64 -> FLOAT64 widening is AUTO,
    # but the run's overall decision is the most severe change in it.
    header_v3 = [
        "confidence_code",
        "country_code",
        "segment_id",
        "year",
        "volume_units",
        "average_selling_price_usd",
        "value_usd",
        "growth_pct",
        "analyst_note",
    ]
    rows_v3 = []
    for i, r in enumerate(data):
        vol = r["volume_units"] + 0.5 if i == 0 else r["volume_units"]
        rows_v3.append(
            [
                conf[r["segment_id"]],
                r["country_code"],
                r["segment_id"],
                r["year"],
                vol,
                r["asp_usd"],
                r["value_usd"],
                r["growth_pct"],
                r["analyst_note"],
            ]
        )
    write("market_inputs_v3_2026-03-02.csv", header_v3, rows_v3, "2026-03-02")

    # ---- v4: someone splits segment out into its own tab ----
    # BLOCK: a required column and part of the grain is gone. Nothing lands.
    header_v4 = [c for c in header if c != "segment_id"]
    write(
        "market_inputs_v4_2026-04-06_breaking.csv",
        header_v4,
        [[r[c] for c in header_v4] for r in data],
        "2026-04-06",
    )

    # ---- the legacy deliverable, frozen as the reconciliation baseline ----
    # Four deliberate discrepancies, one of each kind the diff has to classify.
    recon_header = ["country_code", "segment_id", "year", "value_usd"]
    baseline = []
    for r in data:
        value = r["value_usd"]
        key = (r["country_code"], r["segment_id"], r["year"])
        if key == ("MY", "IVD-IMMUNO", 2025):
            value = round(value * 1.0004, 2)   # inside 0.1% tolerance -> match
        if key == ("TH", "IVD-MOLEC", 2026):
            value = round(value * 1.082, 2)    # 8.2% out -> variance
        if key == ("SG", "IVD-MOLEC", 2025):
            continue                            # absent here -> extra_in_new
        baseline.append([r["country_code"], r["segment_id"], r["year"], value])
    # a row the legacy sheet has that the model does not produce
    baseline.append(["SG", "IVD-LEGACY", 2025, 88000.00])
    write("legacy_market_size_baseline.csv", recon_header, baseline, "2026-01-05", preamble=False)

    candidate = [
        [r["country_code"], r["segment_id"], r["year"], r["value_usd"]] for r in data
    ]
    write(
        "pipeline_market_size_candidate.csv", recon_header, candidate, "2026-03-02",
        preamble=False,
    )


if __name__ == "__main__":
    main()
