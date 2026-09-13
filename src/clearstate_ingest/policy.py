"""Change classes and the policy that maps each one to an action.

The policy is data, not code, so a contract can loosen or tighten any single
class without touching the engine. Defaults are deliberately conservative in one
direction only: anything that could *lose* or *misrepresent* data needs a human;
anything purely additive does not.
"""

from __future__ import annotations

from enum import Enum
from typing import Dict, Mapping, Optional


class ChangeType(str, Enum):
    COLUMN_ADDED = "column_added"
    COLUMN_REMOVED_OPTIONAL = "column_removed_optional"
    COLUMN_REMOVED_REQUIRED = "column_removed_required"
    COLUMN_RENAMED_DECLARED = "column_renamed_declared"
    COLUMN_RENAMED_INFERRED = "column_renamed_inferred"
    COLUMN_REORDERED = "column_reordered"
    TYPE_WIDENED = "type_widened"
    TYPE_NARROWED = "type_narrowed"
    TYPE_INCOMPATIBLE = "type_incompatible"
    REQUIRED_RELAXED = "required_relaxed"
    GRAIN_COLUMN_MISSING = "grain_column_missing"

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return self.value


class Action(str, Enum):
    """What the pipeline does when a change of this class is seen."""

    AUTO = "auto"        # evolve the contract and the table, keep loading
    PROPOSE = "propose"  # land to quarantine, send a contract patch to the owner
    BLOCK = "block"      # write nothing, fail the run, alert the owner

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return self.value


#: Severity order - the run's overall action is the max across all changes.
SEVERITY: Dict[Action, int] = {Action.AUTO: 0, Action.PROPOSE: 1, Action.BLOCK: 2}


DEFAULT_POLICY: Dict[ChangeType, Action] = {
    # purely additive or purely cosmetic - safe to absorb
    ChangeType.COLUMN_ADDED: Action.AUTO,
    ChangeType.COLUMN_REORDERED: Action.AUTO,
    ChangeType.TYPE_WIDENED: Action.AUTO,
    ChangeType.COLUMN_RENAMED_DECLARED: Action.AUTO,
    # plausible but unproven - a human confirms before it reaches core
    ChangeType.REQUIRED_RELAXED: Action.PROPOSE,
    ChangeType.COLUMN_RENAMED_INFERRED: Action.PROPOSE,
    ChangeType.TYPE_NARROWED: Action.PROPOSE,
    ChangeType.COLUMN_REMOVED_OPTIONAL: Action.PROPOSE,
    # data loss or a broken model - never absorbed silently
    ChangeType.COLUMN_REMOVED_REQUIRED: Action.BLOCK,
    ChangeType.TYPE_INCOMPATIBLE: Action.BLOCK,
    ChangeType.GRAIN_COLUMN_MISSING: Action.BLOCK,
}


class Policy:
    """Default policy with per-contract overrides."""

    def __init__(self, overrides: Optional[Mapping[str, str]] = None) -> None:
        self._map: Dict[ChangeType, Action] = dict(DEFAULT_POLICY)
        self.overrides: Dict[ChangeType, Action] = {}
        for key, value in (overrides or {}).items():
            change = ChangeType(key)
            action = Action(value)
            self._map[change] = action
            self.overrides[change] = action

    def action_for(self, change: ChangeType) -> Action:
        return self._map[change]

    def to_dict(self) -> Dict[str, str]:
        return {k.value: v.value for k, v in sorted(self._map.items(), key=lambda kv: kv[0].value)}

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        if not self.overrides:
            return "Policy(default)"
        pairs = ", ".join("{}={}".format(k.value, v.value) for k, v in self.overrides.items())
        return "Policy({})".format(pairs)
