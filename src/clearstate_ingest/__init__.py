"""clearstate_ingest - config-driven ingestion with first-class schema evolution.

The reusable half of the migration factory (design/architecture.md §11): onboarding
a new spreadsheet source should be a YAML file, not a Python module.

    from clearstate_ingest import load_contract, Pipeline, SQLiteWarehouse

    contract = load_contract("sources/market_inputs.yaml")
    pipeline = Pipeline(SQLiteWarehouse("warehouse.db"))
    result = pipeline.run(contract, base_dir=".")
    print(result.summary())
"""

from .contracts import (
    Contract,
    ContractError,
    ReconSpec,
    load_contract,
    load_contracts,
    parse_contract,
    save_contract,
)
from .evolution import EvolutionPlan, SchemaChange, adopt_alias, apply_plan, plan_evolution
from .extract import SourceData, read_csv, read_excel, read_source
from .policy import DEFAULT_POLICY, Action, ChangeType, Policy
from .recon import ReconResult, RowDiff, reconcile, scorecard
from .runner import Pipeline, RunResult, new_load_id
from .schema import (
    DType,
    Field,
    Schema,
    coerce,
    infer_dtype,
    infer_schema,
    is_narrowing,
    is_widening,
    normalise,
)
from .sqlgen import dataform_sqlx, landing_table_ddl, recon_sql, staging_view_sql
from .validate import ValidationReport, Violation, canonicalise, validate
from .warehouse import BigQueryWarehouse, SQLiteWarehouse, Warehouse

__version__ = "0.1.0"

__all__ = [
    "Action",
    "BigQueryWarehouse",
    "ChangeType",
    "Contract",
    "ContractError",
    "DEFAULT_POLICY",
    "DType",
    "EvolutionPlan",
    "Field",
    "Pipeline",
    "Policy",
    "ReconResult",
    "ReconSpec",
    "RowDiff",
    "RunResult",
    "Schema",
    "SchemaChange",
    "SourceData",
    "SQLiteWarehouse",
    "ValidationReport",
    "Violation",
    "Warehouse",
    "adopt_alias",
    "apply_plan",
    "canonicalise",
    "coerce",
    "dataform_sqlx",
    "infer_dtype",
    "infer_schema",
    "is_narrowing",
    "is_widening",
    "landing_table_ddl",
    "load_contract",
    "load_contracts",
    "new_load_id",
    "normalise",
    "parse_contract",
    "plan_evolution",
    "recon_sql",
    "reconcile",
    "save_contract",
    "scorecard",
    "staging_view_sql",
    "validate",
    "__version__",
]
