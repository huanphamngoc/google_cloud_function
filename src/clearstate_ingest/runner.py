"""The run: the seven steps from design/architecture.md §06, in one place.

Idempotent by ``load_id`` - re-running the same load_id replaces its rows rather
than duplicating them, which is what makes a retry safe at any step.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence

from .contracts import Contract, save_contract
from .evolution import EvolutionPlan, apply_plan, plan_evolution
from .extract import SourceData, read_source
from .policy import Action
from .recon import ReconResult, reconcile, scorecard
from .schema import Schema
from .validate import ValidationReport, canonicalise, validate
from .warehouse import Warehouse


def new_load_id(source_id: str) -> str:
    stamp = _dt.datetime.utcnow().strftime("%Y%m%dT%H%M%S")
    return "{}-{}-{}".format(source_id, stamp, uuid.uuid4().hex[:6])


def _utcnow() -> str:
    return _dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _row_hash(row: Mapping[str, Any]) -> str:
    payload = json.dumps({k: v for k, v in row.items() if k != "_raw"}, sort_keys=True, default=str)
    return hashlib.md5(payload.encode("utf-8")).hexdigest()[:16]


@dataclass
class RunResult:
    load_id: str
    source_id: str
    status: str = "pending"          # success | quarantined | blocked | failed
    plan: Optional[EvolutionPlan] = None
    report: Optional[ValidationReport] = None
    evolved_contract: Optional[Contract] = None
    rows_in: int = 0
    rows_loaded: int = 0
    rows_quarantined: int = 0
    messages: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "success"

    def log(self, message: str) -> None:
        self.messages.append(message)

    def summary(self) -> str:
        head = "run {} [{}] {} - {} in, {} loaded".format(
            self.load_id, self.status.upper(), self.source_id, self.rows_in, self.rows_loaded
        )
        if self.rows_quarantined:
            head += ", {} quarantined".format(self.rows_quarantined)
        return "\n".join([head] + ["  " + m for m in self.messages])


class Pipeline:
    """Wires a warehouse, a landing table naming convention and the ops log."""

    def __init__(
        self,
        warehouse: Warehouse,
        landing_prefix: str = "landing_raw_",
        quarantine_prefix: str = "landing_quarantine_",
        contracts_dir: Optional[str] = None,
    ) -> None:
        self.wh = warehouse
        self.landing_prefix = landing_prefix
        self.quarantine_prefix = quarantine_prefix
        self.contracts_dir = contracts_dir

    # -- naming ------------------------------------------------------------
    def landing_table(self, contract: Contract) -> str:
        return "{}{}".format(self.landing_prefix, contract.source_id)

    def quarantine_table(self, contract: Contract) -> str:
        return "{}{}".format(self.quarantine_prefix, contract.source_id)

    # -- ops ---------------------------------------------------------------
    def _open_run(self, load_id: str, source_id: str) -> None:
        # a retry reuses its load_id, so clear any prior attempt first - the whole
        # run is idempotent by load_id and the ops log has to be too
        self.wh.query("DELETE FROM ops_run_log WHERE load_id = ?", (load_id,))
        self.wh.query(
            "INSERT INTO ops_run_log (load_id, source_id, started_at, status) "
            "VALUES (?, ?, ?, 'running')",
            (load_id, source_id, _utcnow()),
        )

    def _close_run(self, result: RunResult) -> None:
        self.wh.query(
            "UPDATE ops_run_log SET finished_at = ?, status = ?, rows_in = ?, "
            "rows_loaded = ?, detail = ? WHERE load_id = ?",
            (
                _utcnow(),
                result.status,
                result.rows_in,
                result.rows_loaded,
                " | ".join(result.messages)[:2000],
                result.load_id,
            ),
        )

    def _register_schema(self, contract: Contract, observed: Schema) -> bool:
        """Record the observed fingerprint. Returns True if it was already known."""
        fp = observed.fingerprint()
        existing = self.wh.query(
            "SELECT fingerprint FROM ops_schema_registry WHERE source_id = ? AND fingerprint = ?",
            (contract.source_id, fp),
        )
        now = _utcnow()
        if existing:
            self.wh.query(
                "UPDATE ops_schema_registry SET last_seen_at = ? "
                "WHERE source_id = ? AND fingerprint = ?",
                (now, contract.source_id, fp),
            )
            return True
        self.wh.query(
            "INSERT INTO ops_schema_registry (source_id, fingerprint, contract_version, "
            "first_seen_at, last_seen_at, schema_json) VALUES (?, ?, ?, ?, ?, ?)",
            (
                contract.source_id,
                fp,
                contract.version,
                now,
                now,
                json.dumps(observed.to_dict()),
            ),
        )
        return False

    def _log_evolution(self, load_id: str, contract: Contract, plan: EvolutionPlan, to_version: int) -> None:
        now = _utcnow()
        for change in plan.changes:
            self.wh.query(
                "INSERT INTO ops_schema_evolution_log (load_id, source_id, decided_at, decision, "
                "from_version, to_version, change_type, column_name, detail, confidence) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    load_id,
                    contract.source_id,
                    now,
                    change.action.value,
                    contract.version,
                    to_version,
                    change.change_type.value,
                    change.column,
                    change.detail,
                    change.confidence,
                ),
            )

    def _log_dq(self, load_id: str, contract: Contract, report: ValidationReport) -> None:
        now = _utcnow()
        counts = report.counts_by_rule() or {"all": 0}
        for rule, n in counts.items():
            self.wh.query(
                "INSERT INTO ops_dq_results (load_id, source_id, checked_at, rule, "
                "column_name, violations, rows_in, rows_ok) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (load_id, contract.source_id, now, rule, "*", n, report.rows_in, report.rows_ok),
            )

    # -- the run -----------------------------------------------------------
    def run(
        self,
        contract: Contract,
        data: Optional[SourceData] = None,
        base_dir: str = ".",
        load_id: Optional[str] = None,
        auto_apply: bool = True,
        write_contract_to: Optional[str] = None,
    ) -> RunResult:
        load_id = load_id or new_load_id(contract.source_id)
        result = RunResult(load_id=load_id, source_id=contract.source_id)
        self._open_run(load_id, contract.source_id)

        # 2 - extract
        if data is None:
            data = read_source(contract, base_dir=base_dir)
        result.rows_in = len(data)
        rows = data.as_dicts()

        # 3 - observe and register the schema
        observed = data.infer()
        known = self._register_schema(contract, observed)
        if known:
            result.log("schema fingerprint {} already registered".format(observed.fingerprint()))

        # 4 - plan evolution
        plan = plan_evolution(contract, observed, sample_rows=rows[:200])
        result.plan = plan
        if plan.is_noop:
            result.log("no schema change")
        else:
            result.log(plan.summary())

        target_version = contract.version + (0 if plan.is_noop else 1)
        self._log_evolution(load_id, contract, plan, target_version)

        # 5 - decide
        if plan.action == Action.BLOCK:
            result.status = "blocked"
            result.log(
                "BLOCKED before writing any row. Suggested contract patch:\n"
                + json.dumps(plan.contract_patch(), indent=2)
            )
            self._close_run(result)
            return result

        working = contract
        if plan.action == Action.AUTO and auto_apply and not plan.is_noop:
            working = apply_plan(contract, plan)
            result.evolved_contract = working
            result.log(
                "contract auto-evolved v{} -> v{}".format(contract.version, working.version)
            )
            if write_contract_to:
                save_contract(working, write_contract_to)
                result.log("contract written to {}".format(write_contract_to))

        # 6 - canonicalise and validate
        canonical = canonicalise(rows, plan)
        report = validate(working, canonical)
        result.report = report
        result.log(report.summary())
        self._log_dq(load_id, working, report)

        # 7 - load
        quarantine = plan.action == Action.PROPOSE
        table = self.quarantine_table(working) if quarantine else self.landing_table(working)
        extra = ["_quarantine_reason TEXT"] if quarantine else []
        self.wh.ensure_table(table, working.schema, extra_columns=extra)
        self.wh.sync_schema(table, working.schema)
        self.wh.delete_load(table, load_id)

        now = _utcnow()
        payload: List[Dict[str, Any]] = []
        for row in report.clean_rows:
            enriched = dict(row)
            enriched.update(
                {
                    "load_id": load_id,
                    "_ingested_at": now,
                    "_row_hash": _row_hash(row),
                    "_source_hash": data.content_hash,
                    "_contract_version": working.version,
                }
            )
            if quarantine:
                enriched["_quarantine_reason"] = "; ".join(
                    c.detail for c in plan.changes_of(Action.PROPOSE)
                )[:500]
            payload.append(enriched)

        result.rows_loaded = self.wh.insert(table, payload)

        if report.rejected_rows:
            reject_table = self.quarantine_table(working)
            self.wh.ensure_table(
                reject_table, working.schema, extra_columns=["_quarantine_reason TEXT"]
            )
            self.wh.sync_schema(reject_table, working.schema)
            rejects = []
            for row in report.rejected_rows:
                enriched = dict(row)
                enriched.update(
                    {
                        "load_id": load_id,
                        "_ingested_at": now,
                        "_row_hash": _row_hash(row),
                        "_source_hash": data.content_hash,
                        "_contract_version": working.version,
                        "_quarantine_reason": row.get("_reject_reason", "validation"),
                    }
                )
                enriched.pop("_reject_reason", None)
                rejects.append(enriched)
            result.rows_quarantined = self.wh.insert(reject_table, rejects)

        if quarantine:
            result.status = "quarantined"
            result.log(
                "loaded to QUARANTINE - downstream still reads the last good partition.\n"
                "Suggested contract patch:\n" + json.dumps(plan.contract_patch(), indent=2)
            )
        elif report.ok or contract.on_violation == "warn":
            result.status = "success"
        elif contract.on_violation == "quarantine":
            result.status = "success"
            result.log("invalid rows quarantined per on_violation=quarantine")
        else:
            result.status = "failed"
            result.log("on_violation=fail_and_notify and there were violations")

        self._close_run(result)
        return result

    # -- reconciliation ----------------------------------------------------
    def reconcile_against(
        self,
        contract: Contract,
        baseline: Sequence[Mapping[str, Any]],
        candidate: Sequence[Mapping[str, Any]],
        load_id: str = "",
    ) -> ReconResult:
        spec = contract.recon
        result = reconcile(
            baseline=baseline,
            candidate=candidate,
            grain=contract.grain,
            measures=spec.measures,
            abs_tolerance=spec.abs_tolerance,
            rel_tolerance=spec.rel_tolerance,
            threshold=spec.match_rate_threshold,
            dataset=contract.source_id,
        )
        card = scorecard(result)
        self.wh.query(
            "INSERT INTO ops_recon_results (load_id, dataset, checked_at, match_rate, "
            "threshold, passed, comparisons, value_delta, scorecard_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                load_id,
                contract.source_id,
                _utcnow(),
                result.match_rate,
                result.threshold,
                int(result.passed),
                result.total,
                result.value_delta(),
                json.dumps(card),
            ),
        )
        return result
