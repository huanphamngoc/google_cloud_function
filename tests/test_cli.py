"""CLI behaviour, including the exit codes CI branches on."""

from __future__ import annotations

import json
import os

import pytest

from clearstate_ingest.cli import EXIT_BLOCK, EXIT_OK, EXIT_PROPOSE, main

CONTRACT = os.path.join("sources", "market_inputs.yaml")


@pytest.fixture(autouse=True)
def in_root(root, monkeypatch):
    monkeypatch.chdir(root)


class TestPlanCommand:
    @pytest.mark.parametrize(
        "version,expected",
        [
            ("v1_2026-01-05", EXIT_OK),
            ("v2_2026-02-02", EXIT_OK),
            ("v3_2026-03-02", EXIT_PROPOSE),
            ("v4_2026-04-06_breaking", EXIT_BLOCK),
        ],
    )
    def test_exit_code_encodes_the_decision(self, version, expected):
        """CI gates on this: 0 ship, 1 needs review, 2 stop."""
        code = main(
            [
                "plan",
                CONTRACT,
                "--file",
                "sample_data/market_inputs_{}.csv".format(version),
            ]
        )
        assert code == expected

    def test_json_output_is_machine_readable(self, capsys):
        main(["plan", CONTRACT, "--file", "sample_data/market_inputs_v3_2026-03-02.csv", "--json"])
        payload = json.loads(capsys.readouterr().out)
        assert payload["decision"] == "propose"
        assert payload["source_id"] == "market_inputs"
        assert any(c["type"] == "column_renamed_inferred" for c in payload["changes"])
        assert payload["patch"]["add_aliases"]["asp_usd"] == ["average_selling_price_usd"]

    def test_plan_writes_nothing(self, tmp_path):
        db = str(tmp_path / "should_not_exist.db")
        main(["plan", CONTRACT, "--file", "sample_data/market_inputs_v2_2026-02-02.csv"])
        assert not os.path.exists(db)


class TestRunCommand:
    def test_run_loads_and_reports(self, tmp_path, capsys):
        db = str(tmp_path / "wh.db")
        code = main(
            [
                "run",
                CONTRACT,
                "--file",
                "sample_data/market_inputs_v1_2026-01-05.csv",
                "--db",
                db,
            ]
        )
        assert code == EXIT_OK
        assert "18 loaded" in capsys.readouterr().out
        assert os.path.exists(db)

    def test_blocked_run_returns_the_block_code(self, tmp_path):
        code = main(
            [
                "run",
                CONTRACT,
                "--file",
                "sample_data/market_inputs_v4_2026-04-06_breaking.csv",
                "--db",
                str(tmp_path / "wh.db"),
            ]
        )
        assert code == EXIT_BLOCK

    def test_no_auto_evolve_downgrades_a_safe_change(self, tmp_path):
        """--no-auto-evolve is the dry-run posture: never mutate the contract."""
        code = main(
            [
                "run",
                CONTRACT,
                "--file",
                "sample_data/market_inputs_v2_2026-02-02.csv",
                "--db",
                str(tmp_path / "wh.db"),
                "--no-auto-evolve",
            ]
        )
        assert code == EXIT_OK


class TestReconCommand:
    def test_recon_reports_and_fails_the_gate(self, capsys):
        code = main(
            [
                "recon",
                CONTRACT,
                "--baseline",
                "sample_data/legacy_market_size_baseline.csv",
                "--candidate",
                "sample_data/pipeline_market_size_candidate.csv",
            ]
        )
        out = capsys.readouterr().out
        assert code == EXIT_PROPOSE
        assert "FAIL" in out
        assert "top variances" in out

    def test_recon_json(self, capsys):
        main(
            [
                "recon",
                CONTRACT,
                "--baseline",
                "sample_data/legacy_market_size_baseline.csv",
                "--candidate",
                "sample_data/pipeline_market_size_candidate.csv",
                "--json",
            ]
        )
        card = json.loads(capsys.readouterr().out)
        assert card["comparisons"] == 19
        assert card["passed"] is False


class TestSqlCommand:
    @pytest.mark.parametrize(
        "what,needle",
        [
            ("landing_ddl", "PARTITION BY DATE(_ingested_at)"),
            ("staging_view", "CREATE OR REPLACE VIEW"),
            ("recon", "FULL OUTER JOIN"),
            ("dataform", "uniqueKey"),
        ],
    )
    def test_each_artifact_is_emitted(self, what, needle, capsys):
        assert main(["sql", CONTRACT, "--what", what]) == EXIT_OK
        assert needle in capsys.readouterr().out


class TestAdoptCommand:
    def test_adopt_writes_the_alias_and_bumps_the_version(self, tmp_path, capsys):
        import shutil

        target = str(tmp_path / "market_inputs.yaml")
        shutil.copy(CONTRACT, target)

        assert main(["adopt", target, "--column", "asp_usd", "--alias", "avg_price"]) == EXIT_OK
        assert "v1 -> v2" in capsys.readouterr().out

        from clearstate_ingest import load_contract

        updated = load_contract(target)
        assert updated.version == 2
        assert updated.schema.get("avg_price").name == "asp_usd"


class TestErrorHandling:
    def test_a_missing_file_is_an_error_not_a_traceback(self, capsys):
        code = main(["plan", CONTRACT, "--file", "sample_data/nope.csv"])
        assert code == 3
        assert "error:" in capsys.readouterr().err
