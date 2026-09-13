"""Command line interface.

    clearstate-ingest plan     sources/market_inputs.yaml --file sample_data/...csv
    clearstate-ingest run      sources/market_inputs.yaml --file ... --db warehouse.db
    clearstate-ingest recon    sources/market_inputs.yaml --baseline a.csv --candidate b.csv
    clearstate-ingest sql      sources/market_inputs.yaml --what staging_view
    clearstate-ingest adopt    sources/market_inputs.yaml --column asp_usd --alias avg_price
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Dict, List, Optional, Sequence

from .contracts import load_contract, save_contract
from .evolution import adopt_alias, plan_evolution
from .extract import read_csv, read_source
from .policy import Action
from .recon import reconcile, scorecard
from .runner import Pipeline
from .sqlgen import dataform_sqlx, landing_table_ddl, recon_sql, staging_view_sql
from .warehouse import SQLiteWarehouse

EXIT_OK = 0
EXIT_PROPOSE = 1
EXIT_BLOCK = 2
EXIT_ERROR = 3


def _load_data(contract, file: Optional[str], base_dir: str):
    if file:
        header_row = int(dict(contract.location).get("header_row", 1))
        data = read_csv(file, header_row=header_row)
        data.source_id = contract.source_id
        return data
    return read_source(contract, base_dir=base_dir)


def cmd_plan(args: argparse.Namespace) -> int:
    contract = load_contract(args.contract)
    data = _load_data(contract, args.file, args.base_dir)
    plan = plan_evolution(contract, data.infer(), sample_rows=data.as_dicts()[:200])

    if args.json:
        print(
            json.dumps(
                {
                    "source_id": contract.source_id,
                    "contract_version": contract.version,
                    "fingerprint": plan.fingerprint,
                    "decision": plan.action.value,
                    "changes": [
                        {
                            "type": c.change_type.value,
                            "action": c.action.value,
                            "column": c.column,
                            "detail": c.detail,
                            "confidence": c.confidence,
                        }
                        for c in plan.changes
                    ],
                    "patch": plan.contract_patch(),
                },
                indent=2,
            )
        )
    else:
        print(plan.summary())
        if plan.action != Action.AUTO:
            print("\nSuggested contract patch:")
            print(json.dumps(plan.contract_patch(), indent=2))

    return {Action.AUTO: EXIT_OK, Action.PROPOSE: EXIT_PROPOSE, Action.BLOCK: EXIT_BLOCK}[
        plan.action
    ]


def cmd_run(args: argparse.Namespace) -> int:
    contract = load_contract(args.contract)
    data = _load_data(contract, args.file, args.base_dir)
    with SQLiteWarehouse(args.db) as wh:
        pipeline = Pipeline(wh)
        result = pipeline.run(
            contract,
            data=data,
            base_dir=args.base_dir,
            auto_apply=not args.no_auto_evolve,
            write_contract_to=args.contract if args.write_contract else None,
        )
        print(result.summary())
    return {
        "success": EXIT_OK,
        "quarantined": EXIT_PROPOSE,
        "blocked": EXIT_BLOCK,
        "failed": EXIT_ERROR,
    }.get(result.status, EXIT_ERROR)


def cmd_recon(args: argparse.Namespace) -> int:
    contract = load_contract(args.contract)
    baseline = read_csv(args.baseline).as_dicts()
    candidate = read_csv(args.candidate).as_dicts()
    result = reconcile(
        baseline=baseline,
        candidate=candidate,
        grain=contract.grain,
        measures=contract.recon.measures,
        abs_tolerance=contract.recon.abs_tolerance,
        rel_tolerance=contract.recon.rel_tolerance,
        threshold=contract.recon.match_rate_threshold,
        dataset=contract.source_id,
    )
    if args.json:
        print(json.dumps(scorecard(result), indent=2))
    else:
        print(result.summary())
        top = result.top_variances(args.top)
        if top:
            print("\n  top variances:")
            for d in top:
                pct = "" if d.pct_diff is None else " ({:+.2%})".format(d.pct_diff)
                print(
                    "    {:<28} {:>14} -> {:>14}  {}{}".format(
                        "/".join(str(k) for k in d.key),
                        "-" if d.baseline is None else "{:,.2f}".format(d.baseline),
                        "-" if d.candidate is None else "{:,.2f}".format(d.candidate),
                        d.status,
                        pct,
                    )
                )
    return EXIT_OK if result.passed else EXIT_PROPOSE


def cmd_sql(args: argparse.Namespace) -> int:
    contract = load_contract(args.contract)
    landing = "{}.landing.raw_{}".format(args.project, contract.source_id)
    staging = "{}.staging.stg_{}".format(args.project, contract.source_id)
    if args.what == "landing_ddl":
        print(landing_table_ddl(contract, landing))
    elif args.what == "staging_view":
        print(staging_view_sql(contract, landing, staging))
    elif args.what == "recon":
        print(
            recon_sql(
                contract,
                "{}.recon.baseline_{}".format(args.project, contract.source_id),
                "{}.recon.candidate_{}".format(args.project, contract.source_id),
            )
        )
    elif args.what == "dataform":
        print(dataform_sqlx(contract, staging))
    else:  # pragma: no cover - argparse restricts choices
        return EXIT_ERROR
    return EXIT_OK


def cmd_adopt(args: argparse.Namespace) -> int:
    contract = load_contract(args.contract)
    updated = adopt_alias(contract, args.column, args.alias)
    save_contract(updated, args.contract)
    print(
        "adopted alias '{}' for column '{}' - contract v{} -> v{}".format(
            args.alias, args.column, contract.version, updated.version
        )
    )
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="clearstate-ingest", description=__doc__)
    p.add_argument("--base-dir", default=".", help="root for relative source paths")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("plan", help="classify schema changes without writing anything")
    sp.add_argument("contract")
    sp.add_argument("--file", help="override the contract's source path")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_plan)

    sr = sub.add_parser("run", help="extract, evolve, validate and load")
    sr.add_argument("contract")
    sr.add_argument("--file", help="override the contract's source path")
    sr.add_argument("--db", default="warehouse.db", help="local SQLite warehouse path")
    sr.add_argument("--no-auto-evolve", action="store_true", help="propose even safe changes")
    sr.add_argument("--write-contract", action="store_true", help="persist the evolved contract")
    sr.set_defaults(func=cmd_run)

    sc = sub.add_parser("recon", help="diff a candidate against a frozen baseline")
    sc.add_argument("contract")
    sc.add_argument("--baseline", required=True)
    sc.add_argument("--candidate", required=True)
    sc.add_argument("--top", type=int, default=10)
    sc.add_argument("--json", action="store_true")
    sc.set_defaults(func=cmd_recon)

    ss = sub.add_parser("sql", help="emit BigQuery SQL for this contract")
    ss.add_argument("contract")
    ss.add_argument(
        "--what",
        choices=["landing_ddl", "staging_view", "recon", "dataform"],
        default="staging_view",
    )
    ss.add_argument("--project", default="clearstate-data-dev")
    ss.set_defaults(func=cmd_sql)

    sa = sub.add_parser("adopt", help="confirm an inferred rename by declaring the alias")
    sa.add_argument("contract")
    sa.add_argument("--column", required=True, help="canonical column name")
    sa.add_argument("--alias", required=True, help="the new source-side name")
    sa.set_defaults(func=cmd_adopt)

    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print("error: {}".format(exc), file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
