from __future__ import annotations

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from clearstate_ingest import (  # noqa: E402
    Contract,
    DType,
    Field,
    Schema,
    SQLiteWarehouse,
    load_contract,
    parse_contract,
)

SAMPLE_DIR = os.path.join(ROOT, "sample_data")
SOURCES_DIR = os.path.join(ROOT, "sources")

SAMPLES = {
    "v1": os.path.join(SAMPLE_DIR, "market_inputs_v1_2026-01-05.csv"),
    "v2": os.path.join(SAMPLE_DIR, "market_inputs_v2_2026-02-02.csv"),
    "v3": os.path.join(SAMPLE_DIR, "market_inputs_v3_2026-03-02.csv"),
    "v4": os.path.join(SAMPLE_DIR, "market_inputs_v4_2026-04-06_breaking.csv"),
    "baseline": os.path.join(SAMPLE_DIR, "legacy_market_size_baseline.csv"),
    "candidate": os.path.join(SAMPLE_DIR, "pipeline_market_size_candidate.csv"),
}


@pytest.fixture(scope="session", autouse=True)
def ensure_sample_data():
    """Regenerate the sample files if any are missing, so the suite is self-contained."""
    if all(os.path.exists(p) for p in SAMPLES.values()):
        return
    sys.path.insert(0, SAMPLE_DIR)
    import generate  # type: ignore

    generate.main()


@pytest.fixture
def root() -> str:
    return ROOT


@pytest.fixture
def sample():
    return SAMPLES


@pytest.fixture
def contract() -> Contract:
    return load_contract(os.path.join(SOURCES_DIR, "market_inputs.yaml"))


@pytest.fixture
def warehouse():
    wh = SQLiteWarehouse(":memory:")
    yield wh
    wh.close()


@pytest.fixture
def tiny_contract() -> Contract:
    """A minimal contract for focused unit tests."""
    return parse_contract(
        {
            "source_id": "tiny",
            "grain": ["id"],
            "columns": [
                {"name": "id", "type": "STRING", "required": True},
                {"name": "qty", "type": "INT64", "required": True, "min": 0},
                {"name": "note", "type": "STRING", "required": False},
            ],
        }
    )
