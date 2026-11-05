"""Read-only SQL execution with deterministic guardrails, plus rule backtesting."""
import os

import sqlite3

import time

from pathlib import Path

import pandas as pd

DB_PATH = Path(os.getenv("FSM_DB", Path(__file__).parent / "fsm.db"))
MAX_ROWS = 1000
TIMEOUT_S = 30

#Only reads are allowed. Enforced by SQLite itself, not by the prompt (ADR-005).
_ALLOWED = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}


def _authorizer(action, *_):
    return sqlite3.SQLITE_OK if action in _ALLOWED else sqlite3.SQLITE_DENY


def connect() -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise FileNotFoundError(f"{DB_PATH} not found - run python prepare_db.py first")
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, check_same_thread=False)
    con.set_authorizer(_authorizer)
    return con


def run_sql(sql: str) -> pd.DataFrame:
    """Run one SELECT. Raises on anything else, on timeout, or on SQL errors."""
    con = connect()
    deadline = time.monotonic() + TIMEOUT_S
    con.set_progress_handler(lambda: time.monotonic() > deadline, 10_000)  # non-zero return aborts
    try:
        cur = con.execute(sql)  # sqlite3 refuses multiple statements
        cols = [d[0] for d in cur.description or []]
        return pd.DataFrame(cur.fetchmany(MAX_ROWS), columns=cols)
    finally:
        con.close()


def schema_ddl() -> str:
    con = connect()
    rows = con.execute("SELECT sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL").fetchall()
    con.close()
    return "\n".join(r[0] for r in rows)


def backtest(where_clause: str) -> dict:
    """Score a rule against labelled history. The numbers come from SQL, never from the LLM."""
    if ";" in where_clause:
        raise ValueError("Rule must be a single WHERE clause")
    sql = f"""
    SELECT COUNT()                       AS labelled_flagged,
            COALESCE(SUM(f.is_fraud), 0)   AS fraud_caught,
            (SELECT COUNT() FROM fraud_labels)               AS labelled_total,
            (SELECT SUM(is_fraud) FROM fraud_labels)          AS fraud_total
            FROM (SELECT id FROM transactions WHERE {where_clause}) t
            JOIN fraud_labels f ON f.transaction_id = t.id
    """
    
    r = run_sql(sql).iloc[0].to_dict()
    flagged, caught = int(r["labelled_flagged"]), int(r["fraud_caught"])
    labelled_total, fraud_total = int(r["labelled_total"]), int(r["fraud_total"] or 0)
    base_rate = fraud_total / labelled_total if labelled_total else 0.0
    precision = caught / flagged if flagged else 0.0
    recall = caught / fraud_total if fraud_total else 0.0
    # Fraud is rare (~0.17% of labelled rows), so fixed-decimal rounding wipes out small values.
    # # Round to significant figures instead, and express rates as percentages.
    return {
        "flagged (labelled)": flagged,
        "flagged % of labelled": _sig(100 * flagged / labelled_total) if labelled_total else 0.0,
        "fraud caught": caught,
        "base rate %": _sig(100 * base_rate),
        "precision %": _sig(100 * precision),
        "recall %": _sig(100 * recall),
        "lift vs base rate": _sig(precision / base_rate) if base_rate else None,
    }


def _sig(x: float, digits: int = 3) -> float:
    """Round to significant figures, so 0.0000257 stays 0.0000257 instead of becoming 0.0."""
    return float(f"{x:.{digits}g}")