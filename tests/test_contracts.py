"""Contract parsing, validation of the contract itself, and round-tripping."""

from __future__ import annotations

import os

import pytest
import yaml

from clearstate_ingest import Contract, ContractError, DType, load_contract, parse_contract
from clearstate_ingest.contracts import load_contracts, save_contract


class TestParsing:
    def test_loads_the_shipped_contract(self, contract):
        assert contract.source_id == "market_inputs"
        assert contract.grain == ("country_code", "segment_id", "year")
        assert contract.version == 1
        assert contract.owner
        assert len(contract.schema) == 8

    def test_types_and_flags_are_read(self, contract):
        year = contract.schema.get("year")
        assert year.dtype == DType.INT64
        assert year.required is True
        assert (year.min, year.max) == (2015, 2040)

    def test_declared_alias_is_read(self, contract):
        assert contract.schema.get("average_selling_price").name == "asp_usd"

    def test_recon_spec_is_read(self, contract):
        assert contract.recon.measures == ("value_usd",)
        assert contract.recon.rel_tolerance == 0.001
        assert contract.recon.match_rate_threshold == 0.995
        assert contract.recon.consecutive_clean_runs == 3

    def test_location_settings_are_read(self, contract):
        assert contract.location["header_row"] == 3
        assert contract.location["path"].endswith(".csv")

    def test_names_are_normalised_on_load(self):
        c = parse_contract(
            {
                "source_id": "t",
                "grain": ["Country Code"],
                "columns": [{"name": "Country Code", "type": "STRING", "required": True}],
            }
        )
        assert c.schema.names == ("country_code",)
        assert c.grain == ("country_code",)


class TestContractValidation:
    def test_missing_source_id_is_rejected(self):
        with pytest.raises(ContractError, match="source_id"):
            parse_contract({"columns": [{"name": "a"}]})

    def test_no_columns_is_rejected(self):
        with pytest.raises(ContractError, match="no columns"):
            parse_contract({"source_id": "t", "columns": []})

    def test_grain_must_be_declared(self):
        with pytest.raises(ContractError, match="grain column"):
            parse_contract(
                {
                    "source_id": "t",
                    "grain": ["missing"],
                    "columns": [{"name": "a", "type": "STRING"}],
                }
            )

    def test_recon_measure_must_be_declared(self):
        with pytest.raises(ContractError, match="recon measure"):
            parse_contract(
                {
                    "source_id": "t",
                    "columns": [{"name": "a", "type": "STRING"}],
                    "recon": {"measures": ["nope"]},
                }
            )

    def test_unsupported_type_is_rejected(self):
        with pytest.raises(ContractError, match="unsupported type"):
            parse_contract(
                {"source_id": "t", "columns": [{"name": "a", "type": "BLOB"}]}
            )

    def test_alias_collision_is_rejected(self):
        # two columns claiming the same alias would make mapping ambiguous
        with pytest.raises(ContractError, match="claimed by both"):
            parse_contract(
                {
                    "source_id": "t",
                    "columns": [
                        {"name": "a", "type": "STRING", "aliases": ["shared"]},
                        {"name": "b", "type": "STRING", "aliases": ["shared"]},
                    ],
                }
            )

    def test_unknown_on_violation_is_rejected(self):
        with pytest.raises(ContractError, match="on_violation"):
            parse_contract(
                {
                    "source_id": "t",
                    "on_violation": "explode",
                    "columns": [{"name": "a", "type": "STRING"}],
                }
            )


class TestRoundTrip:
    def test_yaml_round_trip_preserves_the_contract(self, contract, tmp_path):
        path = str(tmp_path / "rt.yaml")
        save_contract(contract, path)
        again = load_contract(path)
        assert again.source_id == contract.source_id
        assert again.grain == contract.grain
        assert again.schema.names == contract.schema.names
        assert again.schema.fingerprint() == contract.schema.fingerprint()
        assert again.recon.measures == contract.recon.measures
        assert again.location["header_row"] == contract.location["header_row"]

    def test_evolved_contract_survives_a_round_trip(self, contract, tmp_path):
        from clearstate_ingest import Field, Schema

        evolved = contract.bumped(
            Schema(contract.schema.fields + (Field("confidence_code", DType.STRING, since_version=2),))
        )
        path = str(tmp_path / "evolved.yaml")
        save_contract(evolved, path)
        again = load_contract(path)
        assert again.version == contract.version + 1
        assert again.schema.get("confidence_code").since_version == 2

    def test_policy_overrides_survive_a_round_trip(self, tmp_path):
        c = parse_contract(
            {
                "source_id": "t",
                "evolution_policy": {"column_added": "block"},
                "columns": [{"name": "a", "type": "STRING"}],
            }
        )
        path = str(tmp_path / "p.yaml")
        save_contract(c, path)
        again = load_contract(path)
        from clearstate_ingest import Action, ChangeType

        assert again.policy.action_for(ChangeType.COLUMN_ADDED) == Action.BLOCK


class TestLoadDirectory:
    def test_loads_every_contract_in_the_sources_folder(self, root):
        contracts = load_contracts(os.path.join(root, "sources"))
        assert "market_inputs" in contracts

    def test_duplicate_source_id_is_rejected(self, tmp_path):
        body = {"source_id": "dup", "columns": [{"name": "a", "type": "STRING"}]}
        for name in ("a.yaml", "b.yaml"):
            with open(str(tmp_path / name), "w", encoding="utf-8") as fh:
                yaml.safe_dump(body, fh)
        with pytest.raises(ContractError, match="duplicate"):
            load_contracts(str(tmp_path))
