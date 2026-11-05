"""Deterministic safety tests - no LLM or API key needed."""
import json
from pathlib import Path

import pytest
import db

UNSAFE = [
	"DELETE FROM fraud_labels",
	"DROP TABLE users",
	"UPDATE cards SET credit_limit_usd_cents = 0",
	"INSERT INTO mcc_codes VALUES (1, 'x')",
	"SELECT 1; DROP TABLE users",
	"ATTACH DATABASE 'evil.db' AS evil",
	"PRAGMA writable_schema = 1",
	"CREATE TABLE x (a)",
]

@pytest.mark.parametrize("sql", UNSAFE)
def test_unsafe_sql_is_blocked(sql):
	with pytest.raises(Exception):
		db.run_sql(sql)

def test_select_works_and_is_capped():
	assert len(db.run_sql("SELECT * FROM transactions")) == db.MAX_ROWS

def test_rule_injection_is_blocked():
	with pytest.raises(Exception):
		db.backtest("1=1; DROP TABLE users")

def test_backtest_metrics_are_sane():
	m = db.backtest("1 = 1") # flag everything -> recall 1, lift 1
	assert m["recall %"] == 100.0 and m["lift vs base rate"] == 1.0
	assert m["precision %"] == m["base rate %"]

def test_backtest_keeps_small_rates():
	m = db.backtest("amount_usd_cents > 500000") # flags only a handful of rows
	assert m["flagged (labelled)"] > 0 and m["flagged % of labelled"] > 0


@pytest.mark.parametrize("case", [c for c in json.loads((Path(__file__).parent.parent / "evals" / "golden.json").read_text()) if c["sql"]], ids=lambda c: c["id"])
def test_golden_reference_sql_runs(case):
	assert not db.run_sql(case["sql"]).empty