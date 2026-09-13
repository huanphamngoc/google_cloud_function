"""Orchestration logic for the Composer DAG.

These run without Airflow installed - the helpers are deliberately kept out of
the DAG file so the decisions that matter (retry vs fail vs skip) can be
tested directly.
"""

import pytest

from scd2_dag_helpers import (
    OUTCOME_FAIL,
    OUTCOME_OK,
    OUTCOME_RETRY,
    OUTCOME_SKIP,
    classify_response,
    freshness_sql,
    one_current_row_sql,
    parse_tabs,
    task_id,
)


def reply(*tabs):
    return {"load_id": "x", "status": "ok", "tabs": list(tabs)}


def tab(name="Sheet1", status="ok", message=""):
    return {"tab": name, "status": status, "message": message}


class TestClassifyResponse:
    def test_a_clean_run_passes(self):
        assert classify_response(reply(tab())) == (OUTCOME_OK, "")

    def test_a_blocked_tab_fails_immediately_rather_than_retrying(self):
        # A duplicate business key is a spreadsheet problem. Retrying cannot
        # fix it, and three retries at five minutes apart delays telling
        # anyone by half an hour.
        outcome, message = classify_response(
            reply(tab(status="blocked", message="2 duplicate business key(s)"))
        )
        assert outcome == OUTCOME_FAIL
        assert "duplicate" in message

    def test_an_errored_tab_is_retried(self):
        # A 5xx or a transient Sheets error is exactly what retries are for.
        outcome, _ = classify_response(reply(tab(status="error", message="503")))
        assert outcome == OUTCOME_RETRY

    def test_blocked_wins_over_error_when_both_are_present(self):
        # Retrying a batch that contains a permanently broken tab just repeats
        # the same partial failure.
        outcome, _ = classify_response(
            reply(tab("A", "error"), tab("B", "blocked", "dup key"))
        )
        assert outcome == OUTCOME_FAIL

    def test_an_http_error_with_no_tab_detail_is_retried(self):
        outcome, message = classify_response({"tabs": []}, http_status=500)
        assert outcome == OUTCOME_RETRY
        assert "500" in message

    def test_an_all_skipped_reply_reads_as_skipped(self):
        # An empty tab is a normal state - a sheet created but not yet filled
        # in - and should show as skipped, not as a silent green tick.
        outcome, _ = classify_response(reply(tab(status="skipped", message="tab is empty")))
        assert outcome == OUTCOME_SKIP

    def test_a_partial_skip_still_counts_as_success(self):
        outcome, _ = classify_response(reply(tab("A"), tab("B", "skipped")))
        assert outcome == OUTCOME_OK

    def test_preflight_does_not_treat_a_dry_run_as_a_skip(self):
        # A dry run reports every tab as skipped by design. Without this, the
        # preflight task would mark itself skipped and take every downstream
        # load task with it.
        body = reply(tab(status="skipped", message="dry run: 3 rows would load"))
        assert classify_response(body, allow_skip=False)[0] == OUTCOME_OK

    def test_preflight_still_catches_a_blocked_tab(self):
        body = reply(tab(status="blocked", message="empty business key"))
        assert classify_response(body, allow_skip=False)[0] == OUTCOME_FAIL

    def test_an_empty_reply_is_not_an_error(self):
        assert classify_response({})[0] == OUTCOME_OK


class TestTaskId:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("customer", "customer"),
            ("Bang khach hang", "Bang_khach_hang"),
            ("fx rate/daily", "fx_rate_daily"),
            ("a  --  b", "a_--_b"),  # hyphens are legal in a task id, spaces are not
            ("!!!", "tab"),
        ],
    )
    def test_titles_become_legal_task_ids(self, raw, expected):
        assert task_id(raw) == expected


class TestParseTabs:
    def test_declared_tabs_are_extracted_with_their_keys(self):
        tabs = parse_tabs(
            {"tabs": {"Sheet1": {"table": "customer", "business_key": ["id"]}}}
        )
        assert tabs == [
            {"tab": "Sheet1", "table": "customer", "mode": "scd2", "business_key": ["id"]}
        ]

    def test_a_scalar_key_becomes_a_list(self):
        tabs = parse_tabs({"tabs": {"A": {"business_key": "id"}}})
        assert tabs[0]["business_key"] == ["id"]

    def test_a_missing_table_name_falls_back_to_the_tab_title(self):
        tabs = parse_tabs({"tabs": {"Don hang": {"business_key": ["id"]}}})
        assert tabs[0]["table"] == "don_hang"

    def test_malformed_config_yields_nothing_rather_than_raising(self):
        # The DAG then degrades to one task that loads everything, instead of
        # appearing in the UI as an import error with no explanation.
        assert parse_tabs("not a mapping") == []
        assert parse_tabs({}) == []
        assert parse_tabs({"tabs": {"A": "not a mapping"}}) == []


class TestVerifySql:
    def test_the_invariant_is_one_current_row_per_key(self):
        sql = one_current_row_sql("proj", "core", "customer", ["id"])
        assert "COUNTIF(`_is_current`) AS n" in sql
        assert "LOGICAL_AND(n = 1)" in sql
        assert "GROUP BY `id`" in sql

    def test_an_empty_table_passes_rather_than_failing_on_null(self):
        # LOGICAL_AND over zero rows is NULL, which BigQueryCheckOperator
        # treats as a failure - a brand new table is not a broken one.
        assert "IFNULL(" in one_current_row_sql("p", "d", "t", ["id"])

    def test_soft_deleted_rows_are_excluded(self):
        # A deleted key legitimately has zero current rows.
        assert "WHERE NOT `_is_deleted`" in one_current_row_sql("p", "d", "t", ["id"])

    def test_composite_keys_group_by_every_column(self):
        sql = one_current_row_sql("p", "d", "fx", ["currency", "rate_date"])
        assert "GROUP BY `currency`, `rate_date`" in sql

    def test_verification_without_a_key_is_refused(self):
        with pytest.raises(ValueError, match="without a business key"):
            one_current_row_sql("p", "d", "t", [])


class TestFreshnessSql:
    def test_it_looks_for_a_recent_successful_run(self):
        sql = freshness_sql("proj", "ops", max_age_hours=26)
        assert "COUNTIF(status = 'ok') > 0" in sql
        assert "INTERVAL 26 HOUR" in sql
