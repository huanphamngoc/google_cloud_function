"""BigQuery SQL generation.

Everything the pipeline needs to emit as SQL lives here as a pure function of a
Contract, so it can be asserted in tests without a warehouse and reviewed in a
pull request as text.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Sequence

from .contracts import Contract
from .schema import DType, Field

#: BigQuery type to use when pulling a value back out of the _raw JSON column
_JSON_CAST: Dict[DType, str] = {
    DType.BOOL: "BOOL",
    DType.INT64: "INT64",
    DType.NUMERIC: "NUMERIC",
    DType.FLOAT64: "FLOAT64",
    DType.DATE: "DATE",
    DType.DATETIME: "DATETIME",
    DType.TIMESTAMP: "TIMESTAMP",
    DType.STRING: "STRING",
}


def landing_table_ddl(
    contract: Contract,
    table: str,
    partition_field: str = "_ingested_at",
) -> str:
    """CREATE TABLE for a landing table, with lineage columns and partitioning."""
    lines = ["  {} {}".format(f.name, f.dtype.value) for f in contract.schema]
    lines += [
        "  load_id STRING NOT NULL",
        "  _ingested_at TIMESTAMP NOT NULL",
        "  _row_hash STRING",
        "  _source_hash STRING",
        "  _contract_version INT64",
        "  _raw JSON",
    ]
    cluster = [g for g in contract.grain][:4]
    ddl = "CREATE TABLE IF NOT EXISTS `{}` (\n{}\n)\nPARTITION BY DATE({})".format(
        table, ",\n".join(lines), partition_field
    )
    if cluster:
        ddl += "\nCLUSTER BY {}".format(", ".join(cluster))
    ddl += "\nOPTIONS (\n  description = {!r},\n  require_partition_filter = TRUE\n);".format(
        "landing table for source '{}' (contract v{})".format(
            contract.source_id, contract.version
        )
    )
    return ddl


def staging_view_sql(contract: Contract, landing_table: str, view_name: str) -> str:
    """The view that insulates ``core`` from everything the source has ever done.

    Two mechanisms, both invisible downstream:

    * **Late adoption back-fill.** A column adopted at version N is NULL in
      partitions written before N - but its value was captured in ``_raw`` from
      the first day it appeared, so COALESCE recovers it retroactively.
    * **Historical aliases.** If a source column was renamed before the alias was
      declared, the old key is still in ``_raw`` and is read from there.
    """
    projections: List[str] = []
    for f in contract.schema:
        fallbacks: List[str] = []
        keys = [f.name] + [a for a in f.aliases]
        if f.since_version > 0 or f.aliases:
            for key in keys:
                fallbacks.append(
                    "SAFE_CAST(JSON_VALUE(_raw, '$.{}') AS {})".format(key, _JSON_CAST[f.dtype])
                )
        if fallbacks:
            projections.append(
                "  COALESCE({}, {}) AS {}".format(f.name, ", ".join(fallbacks), f.name)
            )
        else:
            projections.append("  {}".format(f.name))

    dedupe_keys = ", ".join(contract.grain) if contract.grain else "_row_hash"
    return (
        "CREATE OR REPLACE VIEW `{view}` AS\n"
        "WITH ranked AS (\n"
        "  SELECT\n"
        "    *,\n"
        "    ROW_NUMBER() OVER (\n"
        "      PARTITION BY {keys}\n"
        "      ORDER BY _ingested_at DESC, load_id DESC\n"
        "    ) AS _rn\n"
        "  FROM `{landing}`\n"
        ")\n"
        "SELECT\n"
        "{projections},\n"
        "  load_id,\n"
        "  _ingested_at,\n"
        "  _contract_version\n"
        "FROM ranked\n"
        "WHERE _rn = 1;"
    ).format(
        view=view_name,
        landing=landing_table,
        keys=dedupe_keys,
        projections=",\n".join(projections),
    )


def recon_sql(
    contract: Contract,
    baseline_table: str,
    candidate_table: str,
    measure: Optional[str] = None,
) -> str:
    """The generic diff from §07, materialised for one dataset."""
    measures = [measure] if measure else list(contract.recon.measures)
    if not measures:
        raise ValueError(
            "contract '{}' declares no recon measures".format(contract.source_id)
        )
    m = measures[0]
    grain = list(contract.grain)
    coalesced = ",\n".join(
        "    COALESCE(b.{0}, c.{0}) AS {0}".format(g) for g in grain
    )
    using = ", ".join(grain)
    return (
        "WITH joined AS (\n"
        "  SELECT\n"
        "{coalesced},\n"
        "    b.{m} AS baseline_value,\n"
        "    c.{m} AS candidate_value\n"
        "  FROM `{baseline}` b\n"
        "  FULL OUTER JOIN `{candidate}` c\n"
        "    USING ({using})\n"
        ")\n"
        "SELECT\n"
        "  *,\n"
        "  CASE\n"
        "    WHEN baseline_value  IS NULL THEN 'extra_in_new'\n"
        "    WHEN candidate_value IS NULL THEN 'missing_in_new'\n"
        "    WHEN ABS(candidate_value - baseline_value)\n"
        "         <= GREATEST({abs_tol}, ABS(baseline_value) * {rel_tol}) THEN 'match'\n"
        "    ELSE 'variance'\n"
        "  END AS status,\n"
        "  SAFE_DIVIDE(candidate_value - baseline_value, baseline_value) AS pct_diff\n"
        "FROM joined"
    ).format(
        coalesced=coalesced,
        m=m,
        baseline=baseline_table,
        candidate=candidate_table,
        using=using,
        abs_tol=contract.recon.abs_tolerance,
        rel_tol=contract.recon.rel_tolerance,
    )


def dataform_sqlx(contract: Contract, staging_view: str) -> str:
    """A Dataform staging model with the contract's assertions already wired in."""
    non_null = [f.name for f in contract.schema if f.required]
    assertions = {
        "uniqueKey": list(contract.grain),
        "nonNull": non_null,
    }
    range_checks: List[str] = []
    for f in contract.schema:
        if f.min is not None:
            range_checks.append("{} < {}".format(f.name, f.min))
        if f.max is not None:
            range_checks.append("{} > {}".format(f.name, f.max))
    if range_checks:
        assertions["rowConditions"] = ["NOT ({})".format(c) for c in range_checks]

    import json as _json

    cols = ",\n".join("  {}".format(f.name) for f in contract.schema)
    return (
        "config {{\n"
        '  type: "view",\n'
        '  schema: "staging",\n'
        '  description: "Contract {sid} v{ver}. Owner: {owner}.",\n'
        "  assertions: {assertions}\n"
        "}}\n\n"
        "SELECT\n"
        "{cols},\n"
        "  load_id,\n"
        "  _ingested_at\n"
        "FROM `{view}`\n"
    ).format(
        sid=contract.source_id,
        ver=contract.version,
        owner=contract.owner or "unassigned",
        assertions=_json.dumps(assertions, indent=2).replace("\n", "\n  "),
        cols=cols,
        view=staging_view,
    )
