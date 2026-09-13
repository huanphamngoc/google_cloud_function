"""Source contracts: the YAML declaration that every ingest is validated against.

A contract is the agreement with the sheet owner. It is versioned, and the
evolution engine is the only thing allowed to bump that version automatically.
"""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import yaml

from .policy import Policy
from .schema import DType, Field, Schema, normalise


class ContractError(ValueError):
    """Raised when a contract file is internally inconsistent."""


@dataclass(frozen=True)
class ReconSpec:
    """How this dataset is reconciled against the deliverable it replaces."""

    baseline: Optional[str] = None
    measures: Tuple[str, ...] = ()
    abs_tolerance: float = 0.01
    rel_tolerance: float = 0.001
    match_rate_threshold: float = 0.995
    consecutive_clean_runs: int = 3


@dataclass(frozen=True)
class Contract:
    source_id: str
    kind: str = "csv"
    target: str = ""
    grain: Tuple[str, ...] = ()
    schema: Schema = field(default_factory=Schema)
    version: int = 1
    owner: str = ""
    schedule: str = ""
    sla_hours: Optional[int] = None
    on_violation: str = "fail_and_notify"  # fail_and_notify | quarantine | warn
    policy: Policy = field(default_factory=Policy)
    recon: ReconSpec = field(default_factory=ReconSpec)
    #: source-specific location settings (spreadsheet_id, tab, header_row, path...)
    location: Mapping[str, Any] = field(default_factory=dict)
    #: retained so a round-trip write preserves anything we did not model
    _raw: Mapping[str, Any] = field(default_factory=dict)

    # -- derived -----------------------------------------------------------
    @property
    def required_names(self) -> Tuple[str, ...]:
        return tuple(f.name for f in self.schema if f.required)

    def field_for(self, observed_name: str) -> Optional[Field]:
        return self.schema.get(observed_name)

    def bumped(self, new_schema: Schema) -> "Contract":
        return replace(self, schema=new_schema, version=self.version + 1)

    # -- serialisation -----------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = dict(copy.deepcopy(dict(self._raw)))
        out.update(
            {
                "source_id": self.source_id,
                "kind": self.kind,
                "version": self.version,
                "target": self.target,
                "grain": list(self.grain),
                "owner": self.owner,
                "schedule": self.schedule,
                "on_violation": self.on_violation,
            }
        )
        if self.sla_hours is not None:
            out["sla_hours"] = self.sla_hours
        out.update(dict(self.location))
        if self.policy.overrides:
            out["evolution_policy"] = {k.value: v.value for k, v in self.policy.overrides.items()}
        if self.recon.baseline:
            out["recon"] = {
                "baseline": self.recon.baseline,
                "measures": list(self.recon.measures),
                "abs_tolerance": self.recon.abs_tolerance,
                "rel_tolerance": self.recon.rel_tolerance,
                "match_rate_threshold": self.recon.match_rate_threshold,
                "consecutive_clean_runs": self.recon.consecutive_clean_runs,
            }
        out["columns"] = [_field_to_dict(f) for f in self.schema]
        return out

    def to_yaml(self) -> str:
        return yaml.safe_dump(self.to_dict(), sort_keys=False, default_flow_style=False)


def _field_to_dict(f: Field) -> Dict[str, Any]:
    d: Dict[str, Any] = {"name": f.name, "type": f.dtype.value, "required": f.required}
    if f.aliases:
        d["aliases"] = list(f.aliases)
    if f.description:
        d["description"] = f.description
    if f.min is not None:
        d["min"] = f.min
    if f.max is not None:
        d["max"] = f.max
    if f.regex:
        d["regex"] = f.regex
    if f.allowed:
        d["allowed"] = list(f.allowed)
    if f.since_version:
        d["since_version"] = f.since_version
    return d


_LOCATION_KEYS = (
    "spreadsheet_id",
    "tab",
    "header_row",
    "path",
    "sheet_name",
    "range",
    "delimiter",
    "encoding",
)


def parse_contract(data: Mapping[str, Any]) -> Contract:
    """Build a Contract from a parsed YAML mapping, validating as we go."""
    if "source_id" not in data:
        raise ContractError("contract is missing 'source_id'")

    raw_columns = data.get("columns") or []
    if not raw_columns:
        raise ContractError("contract '{}' declares no columns".format(data["source_id"]))

    fields: List[Field] = []
    seen: Dict[str, str] = {}
    for entry in raw_columns:
        if "name" not in entry:
            raise ContractError("a column in '{}' has no name".format(data["source_id"]))
        name = normalise(entry["name"])
        try:
            dtype = DType(str(entry.get("type", "STRING")).upper())
        except ValueError:
            raise ContractError(
                "column '{}' has unsupported type {!r}".format(name, entry.get("type"))
            )
        aliases = tuple(entry.get("aliases") or ())
        for candidate in (name,) + tuple(normalise(a) for a in aliases):
            if candidate in seen and seen[candidate] != name:
                raise ContractError(
                    "name/alias '{}' is claimed by both '{}' and '{}'".format(
                        candidate, seen[candidate], name
                    )
                )
            seen[candidate] = name
        fields.append(
            Field(
                name=name,
                dtype=dtype,
                required=bool(entry.get("required", False)),
                aliases=aliases,
                description=str(entry.get("description", "")),
                min=entry.get("min"),
                max=entry.get("max"),
                regex=entry.get("regex"),
                allowed=tuple(entry.get("allowed") or ()),
                since_version=int(entry.get("since_version", 0)),
            )
        )

    schema = Schema(tuple(fields))

    grain = tuple(normalise(g) for g in (data.get("grain") or ()))
    for g in grain:
        if schema.get(g) is None:
            raise ContractError("grain column '{}' is not declared in columns".format(g))

    on_violation = str(data.get("on_violation", "fail_and_notify"))
    if on_violation not in ("fail_and_notify", "quarantine", "warn"):
        raise ContractError("unknown on_violation {!r}".format(on_violation))

    recon_raw = data.get("recon") or {}
    recon = ReconSpec(
        baseline=recon_raw.get("baseline"),
        measures=tuple(normalise(m) for m in (recon_raw.get("measures") or ())),
        abs_tolerance=float(recon_raw.get("abs_tolerance", 0.01)),
        rel_tolerance=float(recon_raw.get("rel_tolerance", 0.001)),
        match_rate_threshold=float(recon_raw.get("match_rate_threshold", 0.995)),
        consecutive_clean_runs=int(recon_raw.get("consecutive_clean_runs", 3)),
    )
    for m in recon.measures:
        if schema.get(m) is None:
            raise ContractError("recon measure '{}' is not declared in columns".format(m))

    location = {k: data[k] for k in _LOCATION_KEYS if k in data}

    return Contract(
        source_id=str(data["source_id"]),
        kind=str(data.get("kind", "csv")),
        target=str(data.get("target", "")),
        grain=grain,
        schema=schema,
        version=int(data.get("version", 1)),
        owner=str(data.get("owner", "")),
        schedule=str(data.get("schedule", "")),
        sla_hours=data.get("sla_hours"),
        on_violation=on_violation,
        policy=Policy(data.get("evolution_policy")),
        recon=recon,
        location=location,
        _raw={
            k: v
            for k, v in data.items()
            if k
            not in set(_LOCATION_KEYS)
            | {
                "source_id",
                "kind",
                "target",
                "grain",
                "columns",
                "version",
                "owner",
                "schedule",
                "sla_hours",
                "on_violation",
                "evolution_policy",
                "recon",
            }
        },
    )


def load_contract(path: str) -> Contract:
    with open(path, "r", encoding="utf-8") as fh:
        return parse_contract(yaml.safe_load(fh))


def load_contracts(directory: str) -> Dict[str, Contract]:
    """Load every ``*.yaml`` in ``directory``, keyed by ``source_id``."""
    out: Dict[str, Contract] = {}
    for name in sorted(os.listdir(directory)):
        if not name.endswith((".yaml", ".yml")):
            continue
        contract = load_contract(os.path.join(directory, name))
        if contract.source_id in out:
            raise ContractError("duplicate source_id '{}'".format(contract.source_id))
        out[contract.source_id] = contract
    return out


def save_contract(contract: Contract, path: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(contract.to_yaml())
