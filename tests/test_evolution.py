"""The evolution engine: every change class, and the decision it produces."""

from __future__ import annotations

import pytest

from clearstate_ingest import (
    Action,
    ChangeType,
    DType,
    Field,
    Schema,
    adopt_alias,
    apply_plan,
    parse_contract,
    plan_evolution,
)
from clearstate_ingest.evolution import name_similarity


def observed(**cols) -> Schema:
    """Shorthand: observed(id=DType.STRING, qty=DType.INT64)."""
    return Schema(tuple(Field(name=n, dtype=t) for n, t in cols.items()))


def types_in(plan):
    return {c.change_type for c in plan.changes}


class TestNoChange:
    def test_identical_schema_is_a_noop(self, tiny_contract):
        plan = plan_evolution(
            tiny_contract, observed(id=DType.STRING, qty=DType.INT64, note=DType.STRING)
        )
        assert plan.is_noop
        assert plan.action == Action.AUTO
        assert plan.column_map == {"id": "id", "qty": "qty", "note": "note"}

    def test_reordering_alone_is_not_a_change(self, tiny_contract):
        plan = plan_evolution(
            tiny_contract, observed(note=DType.STRING, qty=DType.INT64, id=DType.STRING)
        )
        # reordering is reported as informational, and stays AUTO
        assert plan.action == Action.AUTO
        assert types_in(plan) <= {ChangeType.COLUMN_REORDERED}

    def test_header_casing_is_not_a_change(self, tiny_contract):
        schema = Schema(
            (Field("ID", DType.STRING), Field("Qty", DType.INT64), Field("Note", DType.STRING))
        )
        plan = plan_evolution(tiny_contract, schema)
        assert plan.is_noop


class TestAutoChanges:
    def test_new_column_is_auto_adopted(self, tiny_contract):
        plan = plan_evolution(
            tiny_contract,
            observed(id=DType.STRING, qty=DType.INT64, note=DType.STRING, region=DType.STRING),
        )
        assert plan.action == Action.AUTO
        assert ChangeType.COLUMN_ADDED in types_in(plan)
        # unmapped until adopted, but recorded so it can be landed in _raw
        assert "region" in plan.unmapped

    def test_widening_is_auto(self, tiny_contract):
        plan = plan_evolution(
            tiny_contract, observed(id=DType.STRING, qty=DType.FLOAT64, note=DType.STRING)
        )
        assert plan.action == Action.AUTO
        change = next(c for c in plan.changes if c.change_type == ChangeType.TYPE_WIDENED)
        assert (change.from_type, change.to_type) == (DType.INT64, DType.FLOAT64)

    def test_declared_alias_is_auto(self):
        contract = parse_contract(
            {
                "source_id": "t",
                "grain": ["id"],
                "columns": [
                    {"name": "id", "type": "STRING", "required": True},
                    {
                        "name": "asp_usd",
                        "type": "FLOAT64",
                        "required": True,
                        "aliases": ["average_selling_price_usd"],
                    },
                ],
            }
        )
        plan = plan_evolution(
            contract, observed(id=DType.STRING, average_selling_price_usd=DType.FLOAT64)
        )
        assert plan.action == Action.AUTO
        assert ChangeType.COLUMN_RENAMED_DECLARED in types_in(plan)
        assert plan.column_map["average_selling_price_usd"] == "asp_usd"


class TestProposeChanges:
    def test_inferred_rename_is_proposed_not_applied(self, tiny_contract):
        plan = plan_evolution(
            tiny_contract,
            observed(id=DType.STRING, quantity=DType.INT64, note=DType.STRING),
        )
        assert plan.action == Action.PROPOSE
        change = next(
            c for c in plan.changes if c.change_type == ChangeType.COLUMN_RENAMED_INFERRED
        )
        assert change.column == "qty"
        assert change.observed_name == "quantity"
        assert change.confidence is not None and change.confidence >= 0.5

    def test_narrowing_is_proposed(self, tiny_contract):
        contract = parse_contract(
            {
                "source_id": "t",
                "grain": ["id"],
                "columns": [
                    {"name": "id", "type": "STRING", "required": True},
                    {"name": "amount", "type": "FLOAT64", "required": True},
                ],
            }
        )
        plan = plan_evolution(contract, observed(id=DType.STRING, amount=DType.INT64))
        assert plan.action == Action.PROPOSE
        assert ChangeType.TYPE_NARROWED in types_in(plan)

    def test_optional_column_disappearing_is_proposed(self, tiny_contract):
        plan = plan_evolution(tiny_contract, observed(id=DType.STRING, qty=DType.INT64))
        assert plan.action == Action.PROPOSE
        assert ChangeType.COLUMN_REMOVED_OPTIONAL in types_in(plan)

    def test_required_column_present_but_entirely_empty(self, tiny_contract):
        plan = plan_evolution(
            tiny_contract,
            observed(id=DType.STRING, qty=DType.INT64, note=DType.STRING),
            sample_rows=[{"id": "a", "qty": None, "note": "x"} for _ in range(5)],
        )
        assert ChangeType.REQUIRED_RELAXED in types_in(plan)
        assert plan.action == Action.PROPOSE


class TestBlockingChanges:
    def test_missing_required_column_blocks(self, tiny_contract):
        plan = plan_evolution(tiny_contract, observed(id=DType.STRING, note=DType.STRING))
        assert plan.action == Action.BLOCK
        assert ChangeType.COLUMN_REMOVED_REQUIRED in types_in(plan)

    def test_missing_grain_column_blocks(self, tiny_contract):
        plan = plan_evolution(tiny_contract, observed(qty=DType.INT64, note=DType.STRING))
        assert plan.action == Action.BLOCK
        assert ChangeType.GRAIN_COLUMN_MISSING in types_in(plan)

    def test_incompatible_type_blocks(self):
        contract = parse_contract(
            {
                "source_id": "t",
                "grain": ["id"],
                "columns": [
                    {"name": "id", "type": "STRING", "required": True},
                    {"name": "when", "type": "DATE", "required": True},
                ],
            }
        )
        plan = plan_evolution(contract, observed(id=DType.STRING, when=DType.INT64))
        assert plan.action == Action.BLOCK
        assert ChangeType.TYPE_INCOMPATIBLE in types_in(plan)

    def test_severity_wins_over_count(self, tiny_contract):
        """One BLOCK outranks any number of AUTO changes."""
        plan = plan_evolution(
            tiny_contract,
            observed(id=DType.STRING, extra1=DType.STRING, extra2=DType.STRING),
        )
        assert plan.action == Action.BLOCK
        assert len(plan.changes_of(Action.AUTO)) >= 1


class TestPolicyOverrides:
    def test_a_contract_can_tighten_a_class(self):
        contract = parse_contract(
            {
                "source_id": "t",
                "grain": ["id"],
                "evolution_policy": {"column_added": "block"},
                "columns": [{"name": "id", "type": "STRING", "required": True}],
            }
        )
        plan = plan_evolution(contract, observed(id=DType.STRING, surprise=DType.STRING))
        assert plan.action == Action.BLOCK

    def test_a_contract_can_loosen_a_class(self):
        contract = parse_contract(
            {
                "source_id": "t",
                "grain": ["id"],
                "evolution_policy": {"column_removed_optional": "auto"},
                "columns": [
                    {"name": "id", "type": "STRING", "required": True},
                    {"name": "note", "type": "STRING", "required": False},
                ],
            }
        )
        plan = plan_evolution(contract, observed(id=DType.STRING))
        assert plan.action == Action.AUTO


class TestApplyPlan:
    def test_auto_plan_bumps_version_and_adds_the_column(self, tiny_contract):
        plan = plan_evolution(
            tiny_contract,
            observed(id=DType.STRING, qty=DType.INT64, note=DType.STRING, region=DType.STRING),
        )
        evolved = apply_plan(tiny_contract, plan)
        assert evolved.version == tiny_contract.version + 1
        added = evolved.schema.get("region")
        assert added is not None
        assert added.required is False
        assert added.since_version == evolved.version

    def test_widening_is_applied_to_the_contract(self, tiny_contract):
        plan = plan_evolution(
            tiny_contract, observed(id=DType.STRING, qty=DType.FLOAT64, note=DType.STRING)
        )
        evolved = apply_plan(tiny_contract, plan)
        assert evolved.schema.get("qty").dtype == DType.FLOAT64

    def test_propose_changes_are_not_applied(self, tiny_contract):
        plan = plan_evolution(
            tiny_contract, observed(id=DType.STRING, quantity=DType.INT64, note=DType.STRING)
        )
        evolved = apply_plan(tiny_contract, plan)
        # the inferred rename is NOT silently adopted as an alias
        assert evolved.schema.get("quantity") is None

    def test_blocking_plan_cannot_be_applied(self, tiny_contract):
        plan = plan_evolution(tiny_contract, observed(id=DType.STRING))
        with pytest.raises(ValueError, match="blocking"):
            apply_plan(tiny_contract, plan)

    def test_noop_returns_the_same_contract(self, tiny_contract):
        plan = plan_evolution(
            tiny_contract, observed(id=DType.STRING, qty=DType.INT64, note=DType.STRING)
        )
        assert apply_plan(tiny_contract, plan) is tiny_contract


class TestAdoptAlias:
    def test_adopting_turns_a_proposal_into_an_auto_change(self, tiny_contract):
        first = plan_evolution(
            tiny_contract, observed(id=DType.STRING, quantity=DType.INT64, note=DType.STRING)
        )
        assert first.action == Action.PROPOSE

        confirmed = adopt_alias(tiny_contract, "qty", "quantity")
        second = plan_evolution(
            confirmed, observed(id=DType.STRING, quantity=DType.INT64, note=DType.STRING)
        )
        assert second.action == Action.AUTO
        assert second.column_map["quantity"] == "qty"
        assert confirmed.version == tiny_contract.version + 1

    def test_unknown_column_raises(self, tiny_contract):
        with pytest.raises(KeyError):
            adopt_alias(tiny_contract, "nope", "x")


class TestSimilarity:
    @pytest.mark.parametrize(
        "a,b",
        [
            ("asp_usd", "average_selling_price_usd"),
            ("qty", "quantity"),
            ("country_code", "countrycode"),
            ("value_usd", "value_in_usd"),
        ],
    )
    def test_plausible_renames_score_above_threshold(self, a, b):
        assert name_similarity(a, b) >= 0.5

    @pytest.mark.parametrize("a,b", [("country_code", "volume_units"), ("year", "analyst_note")])
    def test_unrelated_names_score_below_threshold(self, a, b):
        assert name_similarity(a, b) < 0.5

    def test_similarity_is_symmetric(self):
        assert name_similarity("qty", "quantity") == name_similarity("quantity", "qty")


class TestContractPatch:
    def test_patch_describes_the_edit_for_a_proposal(self, tiny_contract):
        plan = plan_evolution(
            tiny_contract, observed(id=DType.STRING, quantity=DType.INT64, note=DType.STRING)
        )
        patch = plan.contract_patch()
        assert patch["version"] == tiny_contract.version + 1
        assert patch["add_aliases"]["qty"] == ["quantity"]
        assert patch["review"]

    def test_patch_lists_blocking_reasons(self, tiny_contract):
        plan = plan_evolution(tiny_contract, observed(id=DType.STRING))
        assert any("required" in line for line in plan.contract_patch()["review"])
