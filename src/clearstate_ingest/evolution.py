"""The schema evolution engine.

Given a contract and the schema actually observed in an incoming file, produce a
plan: what changed, how each change is classified, what the pipeline should do,
and - when the change is safe - the evolved contract to carry forward.

Three properties this is built to guarantee:

1. **Nothing is ever silently dropped.** Columns the contract does not know about
   are still landed, inside the ``_raw`` JSON column, so a column added today can
   be back-filled for historical partitions the day it is adopted.
2. **Canonical names never change.** A source-side rename is absorbed as an alias,
   so ``core`` models never see it. This is why renames are cheap here.
3. **Ambiguity is escalated, not guessed.** An inferred rename is a proposal with
   a suggested patch, never an applied change.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from .contracts import Contract
from .policy import SEVERITY, Action, ChangeType, Policy
from .schema import (
    DType,
    Field as SField,
    Schema,
    is_narrowing,
    is_nullish,
    is_widening,
    normalise,
)

#: minimum similarity before two names are treated as a possible rename
RENAME_SIMILARITY_THRESHOLD = 0.5


@dataclass(frozen=True)
class SchemaChange:
    change_type: ChangeType
    action: Action
    column: str
    detail: str
    observed_name: Optional[str] = None
    from_type: Optional[DType] = None
    to_type: Optional[DType] = None
    confidence: Optional[float] = None

    def describe(self) -> str:
        return "[{}] {} - {}".format(self.action.value.upper(), self.change_type.value, self.detail)


@dataclass
class EvolutionPlan:
    contract: Contract
    observed: Schema
    changes: List[SchemaChange] = field(default_factory=list)
    #: observed (normalised) column name -> canonical contract column name
    column_map: Dict[str, str] = field(default_factory=dict)
    #: observed columns with no canonical home; preserved in ``_raw``
    unmapped: List[str] = field(default_factory=list)
    fingerprint: str = ""

    @property
    def action(self) -> Action:
        """Overall decision: the most severe action across all changes."""
        if not self.changes:
            return Action.AUTO
        return max((c.action for c in self.changes), key=lambda a: SEVERITY[a])

    @property
    def is_noop(self) -> bool:
        return not self.changes

    def changes_of(self, action: Action) -> List[SchemaChange]:
        return [c for c in self.changes if c.action == action]

    def blocking(self) -> List[SchemaChange]:
        return self.changes_of(Action.BLOCK)

    def summary(self) -> str:
        if self.is_noop:
            return "schema unchanged (fingerprint {})".format(self.fingerprint)
        lines = ["{} change(s), decision = {}".format(len(self.changes), self.action.value.upper())]
        lines.extend("  " + c.describe() for c in self.changes)
        return "\n".join(lines)

    def contract_patch(self) -> Dict[str, Any]:
        """A minimal, paste-able description of what the contract should become.

        Emitted for PROPOSE and BLOCK decisions so the owner sees a concrete edit
        rather than an error message.
        """
        patch: Dict[str, Any] = {
            "source_id": self.contract.source_id,
            "version": self.contract.version + 1,
            "add_columns": [],
            "add_aliases": {},
            "change_types": {},
            "review": [],
        }
        for change in self.changes:
            if change.change_type == ChangeType.COLUMN_ADDED:
                patch["add_columns"].append(
                    {
                        "name": change.column,
                        "type": (change.to_type or DType.STRING).value,
                        "required": False,
                        "since_version": self.contract.version + 1,
                    }
                )
            elif change.change_type in (
                ChangeType.COLUMN_RENAMED_DECLARED,
                ChangeType.COLUMN_RENAMED_INFERRED,
            ):
                patch["add_aliases"].setdefault(change.column, []).append(change.observed_name)
            elif change.change_type in (ChangeType.TYPE_WIDENED, ChangeType.TYPE_NARROWED):
                patch["change_types"][change.column] = (change.to_type or DType.STRING).value
            if change.action in (Action.PROPOSE, Action.BLOCK):
                patch["review"].append(change.describe())
        return {k: v for k, v in patch.items() if v not in ([], {}, None)}


# --------------------------------------------------------------------------
# similarity
# --------------------------------------------------------------------------


def _tokens(name: str) -> Set[str]:
    return {t for t in normalise(name).split("_") if t}


def name_similarity(a: str, b: str) -> float:
    """Blend sequence similarity with token overlap.

    Sequence similarity alone misses ``asp_usd`` -> ``average_selling_price_usd``;
    token overlap alone misses ``country_code`` -> ``countrycode``. Taking the max
    of the two catches both without inventing a bespoke metric.
    """
    na, nb = normalise(a), normalise(b)
    if not na or not nb:
        return 0.0
    seq = difflib.SequenceMatcher(None, na, nb).ratio()
    ta, tb = _tokens(a), _tokens(b)
    overlap = len(ta & tb) / max(1, min(len(ta), len(tb))) if ta and tb else 0.0
    return max(seq, overlap)


# --------------------------------------------------------------------------
# planning
# --------------------------------------------------------------------------


def plan_evolution(
    contract: Contract,
    observed: Schema,
    sample_rows: Optional[Sequence[Mapping[str, Any]]] = None,
) -> EvolutionPlan:
    """Classify every difference between ``contract.schema`` and ``observed``."""
    policy: Policy = contract.policy
    plan = EvolutionPlan(
        contract=contract, observed=observed, fingerprint=observed.fingerprint()
    )

    def add(
        change_type: ChangeType,
        column: str,
        detail: str,
        observed_name: Optional[str] = None,
        from_type: Optional[DType] = None,
        to_type: Optional[DType] = None,
        confidence: Optional[float] = None,
    ) -> None:
        plan.changes.append(
            SchemaChange(
                change_type=change_type,
                action=policy.action_for(change_type),
                column=column,
                detail=detail,
                observed_name=observed_name,
                from_type=from_type,
                to_type=to_type,
                confidence=confidence,
            )
        )

    # ---- pass 1: map observed columns onto contract columns by name/alias ----
    matched_contract: Set[str] = set()
    unmatched_observed: List[SField] = []

    for obs in observed:
        target = contract.schema.get(obs.name)
        if target is None:
            unmatched_observed.append(obs)
            continue
        plan.column_map[normalise(obs.name)] = target.name
        matched_contract.add(target.name)
        if normalise(obs.name) != normalise(target.name):
            add(
                ChangeType.COLUMN_RENAMED_DECLARED,
                target.name,
                "source column '{}' is a declared alias of '{}'".format(obs.name, target.name),
                observed_name=obs.name,
                confidence=1.0,
            )

    unmatched_contract: List[SField] = [f for f in contract.schema if f.name not in matched_contract]

    # ---- pass 2: infer renames among the leftovers ----
    pairs: List[Tuple[float, SField, SField]] = []
    for obs in unmatched_observed:
        for cf in unmatched_contract:
            score = name_similarity(obs.name, cf.name)
            type_ok = obs.dtype == cf.dtype or is_widening(obs.dtype, cf.dtype) or is_widening(
                cf.dtype, obs.dtype
            )
            if score >= RENAME_SIMILARITY_THRESHOLD and type_ok:
                pairs.append((score, obs, cf))

    pairs.sort(key=lambda p: p[0], reverse=True)
    used_obs: Set[str] = set()
    used_con: Set[str] = set()
    for score, obs, cf in pairs:
        if obs.name in used_obs or cf.name in used_con:
            continue
        used_obs.add(obs.name)
        used_con.add(cf.name)
        plan.column_map[normalise(obs.name)] = cf.name
        add(
            ChangeType.COLUMN_RENAMED_INFERRED,
            cf.name,
            "source column '{}' looks like a rename of '{}' (similarity {:.2f}) - "
            "confirm, then declare it as an alias".format(obs.name, cf.name, score),
            observed_name=obs.name,
            confidence=score,
        )

    # ---- pass 3: genuinely new columns ----
    for obs in unmatched_observed:
        if obs.name in used_obs:
            continue
        plan.unmapped.append(normalise(obs.name))
        add(
            ChangeType.COLUMN_ADDED,
            normalise(obs.name),
            "new source column '{}' ({}) - landed in _raw until adopted".format(
                obs.name, obs.dtype.value
            ),
            observed_name=obs.name,
            to_type=obs.dtype,
        )

    # ---- pass 4: disappeared columns ----
    for cf in unmatched_contract:
        if cf.name in used_con:
            continue
        if cf.required:
            add(
                ChangeType.COLUMN_REMOVED_REQUIRED,
                cf.name,
                "required column '{}' is absent from the source".format(cf.name),
                from_type=cf.dtype,
            )
        else:
            add(
                ChangeType.COLUMN_REMOVED_OPTIONAL,
                cf.name,
                "optional column '{}' is absent - will load as NULL".format(cf.name),
                from_type=cf.dtype,
            )

    # ---- pass 5: type drift on mapped columns ----
    for obs in observed:
        canonical = plan.column_map.get(normalise(obs.name))
        if canonical is None:
            continue
        cf = contract.schema.get(canonical)
        if cf is None or obs.dtype == cf.dtype:
            continue
        if is_widening(cf.dtype, obs.dtype):
            add(
                ChangeType.TYPE_WIDENED,
                cf.name,
                "'{}' widened {} -> {}".format(cf.name, cf.dtype.value, obs.dtype.value),
                observed_name=obs.name,
                from_type=cf.dtype,
                to_type=obs.dtype,
            )
        elif is_narrowing(cf.dtype, obs.dtype):
            add(
                ChangeType.TYPE_NARROWED,
                cf.name,
                "'{}' narrowed {} -> {} - values may not fit".format(
                    cf.name, cf.dtype.value, obs.dtype.value
                ),
                observed_name=obs.name,
                from_type=cf.dtype,
                to_type=obs.dtype,
            )
        else:
            add(
                ChangeType.TYPE_INCOMPATIBLE,
                cf.name,
                "'{}' changed {} -> {} with no safe cast".format(
                    cf.name, cf.dtype.value, obs.dtype.value
                ),
                observed_name=obs.name,
                from_type=cf.dtype,
                to_type=obs.dtype,
            )

    # ---- pass 6: required column present but entirely empty ----
    if sample_rows:
        # sample_rows are keyed by SOURCE column name, so an aliased column has to
        # be looked up through the mapping - not by its canonical name
        source_for = {canonical: src for src, canonical in plan.column_map.items()}
        for cf in contract.schema:
            if not cf.required or cf.name not in matched_contract:
                continue
            key = source_for.get(cf.name, cf.name)
            values = [row.get(key) for row in sample_rows]
            if values and all(is_nullish(v) for v in values):
                add(
                    ChangeType.REQUIRED_RELAXED,
                    cf.name,
                    "required column '{}' is present but empty in every row".format(cf.name),
                    from_type=cf.dtype,
                )

    # ---- pass 7: reordering (informational - mapping is always by name) ----
    obs_order = [plan.column_map.get(normalise(o.name)) for o in observed]
    obs_order = [n for n in obs_order if n]
    con_order = [f.name for f in contract.schema if f.name in set(obs_order)]
    if obs_order != con_order and set(obs_order) == set(con_order):
        add(
            ChangeType.COLUMN_REORDERED,
            "*",
            "columns reordered - no effect, mapping is by name",
        )

    # ---- pass 8: the grain must survive, whatever else happened ----
    mapped_canonicals = set(plan.column_map.values())
    for g in contract.grain:
        if g not in mapped_canonicals:
            add(
                ChangeType.GRAIN_COLUMN_MISSING,
                g,
                "grain column '{}' could not be mapped from the source".format(g),
            )

    return plan


# --------------------------------------------------------------------------
# application
# --------------------------------------------------------------------------


def apply_plan(contract: Contract, plan: EvolutionPlan) -> Contract:
    """Return the contract evolved by every AUTO change in ``plan``.

    PROPOSE and BLOCK changes are deliberately *not* applied - they surface as a
    patch for the owner instead. Calling this on a blocking plan raises.
    """
    if plan.action == Action.BLOCK:
        raise ValueError(
            "cannot apply a blocking plan: {}".format(
                "; ".join(c.detail for c in plan.blocking())
            )
        )
    if plan.is_noop:
        return contract

    new_version = contract.version + 1
    fields: List[SField] = list(contract.schema.fields)
    by_name = {f.name: i for i, f in enumerate(fields)}

    for change in plan.changes:
        if change.action != Action.AUTO:
            continue

        if change.change_type == ChangeType.COLUMN_ADDED:
            if change.column in by_name:
                continue
            fields.append(
                SField(
                    name=change.column,
                    dtype=change.to_type or DType.STRING,
                    required=False,
                    description="auto-adopted at contract version {}".format(new_version),
                    since_version=new_version,
                )
            )
            by_name[change.column] = len(fields) - 1

        elif change.change_type == ChangeType.COLUMN_RENAMED_DECLARED:
            # already an alias by definition; nothing to add
            continue

        elif change.change_type == ChangeType.TYPE_WIDENED:
            idx = by_name.get(change.column)
            if idx is not None and change.to_type is not None:
                fields[idx] = replace(fields[idx], dtype=change.to_type)

    return contract.bumped(Schema(tuple(fields)))


def adopt_alias(contract: Contract, canonical: str, alias: str) -> Contract:
    """Confirm an inferred rename by declaring the alias. Bumps the version.

    This is what a reviewer calls after approving a PROPOSE decision; from the
    next run the same rename classifies as COLUMN_RENAMED_DECLARED and is AUTO.
    """
    fields = []
    found = False
    for f in contract.schema:
        if f.name == normalise(canonical):
            fields.append(f.with_alias(normalise(alias)))
            found = True
        else:
            fields.append(f)
    if not found:
        raise KeyError("no column named '{}' in contract '{}'".format(canonical, contract.source_id))
    return contract.bumped(Schema(tuple(fields)))
