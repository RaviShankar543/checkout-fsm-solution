"""NL -> SQL and insight -> rule, using Claude with structured output and a self-repair loop."""
import os

import anthropic
from dotenv import load_dotenv
from pydantic import BaseModel

import db

load_dotenv()
MODEL = os.getenv("FSM_MODEL", "claude-opus-5-5")
MAX_REPAIRS = 2
client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY


class SqlAnswer(BaseModel):
    sql: str
    explanation: str
    assumptions: list[str]


class RuleProposal(BaseModel):
    where_clause: str
    rationale: str

#Domain knowledge the raw DDL does not carry (see "Data traps" in README).
COLUMN_NOTES = """
- All money columns are in US cents: amount_usd_cents, credit_limit_usd_cents, *_income_usd_cents, total_debt_usd_cents. "$1,000" = 100000.
- Negative amount_usd_cents are refunds/reversals.
- transactions.date is TEXT 'YYYY-MM-DD HH:MM:SS'; data covers 2019-01-01 to 2019-10-31. Use strftime() for parts.
- fraud_labels covers only ~67% of transactions. Unlabelled is NOT "not fraud": compute fraud rates by
    INNER JOIN fraud_labels f ON f.transaction_id = t.id, never by treating missing labels as 0.
- Fraud is rare (~0.17% of labelled transactions); report rates, not just counts, and mind small samples.
- transactions.errors is NULL when there was no error.
- cards.card_on_dark_web is always 0 and has no signal.
- Merchant category: transactions.merchant_id -> merchants.mcc -> mcc_codes.description.
- Merchant geography: transactions.merchant_location_id -> merchant_locations (city, state, zip)."""


EXAMPLES = """
Q: What is the fraud rate by transaction type?
SQL: SELECT t.transaction_type, COUNT(*) AS labelled, SUM(f.is_fraud) AS fraud,
    ROUND(100.0 * AVG(f.is_fraud), 3) AS fraud_rate_pct
    FROM transactions t JOIN fraud_labels f ON f.transaction_id = t.id
    GROUP BY 1 ORDER BY fraud_rate_pct DESC

Q: Top 5 merchant categories by fraud rate with at least 500 labelled transactions
SQL: SELECT mc.description, COUNT(*) AS labelled, SUM(f.is_fraud) AS fraud,
    ROUND(100.0 * AVG(f.is_fraud), 3) AS fraud_rate_pct
    FROM transactions t JOIN fraud_labels f ON f.transaction_id = t.id
    JOIN merchants m ON m.id = t.merchant_id JOIN mcc_codes mc ON mc.mcc = m.mcc
    GROUP BY 1 HAVING COUNT(*) >= 500 ORDER BY fraud_rate_pct DESC LIMIT 5
"""


def _system(task: str) -> str:
    return f"""You help a Fraud Success Manager analyse card transactions in SQLite.

{task}

Schema:
{db.schema_ddl()}

Column notes:
{COLUMN_NOTES}

Examples:
{EXAMPLES}

Rules:
- SQLite dialect, a single read-only SELECT. Never modify data.
- Never state numbers yourself; the query result is the answer.
- List every assumption you made (time window, units, how fraud rate is defined, ambiguity in the question).
- If the question cannot be answered from this schema, return sql = "" and explain why."""

def _call(system: str, messages: list, schema):
    resp = client.beta.messages.parse(
        model=MODEL,
        max_tokens=16000,
        system=system,
        messages=messages,
        output_format=schema,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )
    if resp.stop_reason == "refusal":
        raise RuntimeError("Model declined the request")
    return resp.parsed_output

def ask(question: str, history: list[dict] | None = None) -> dict:
    """Returns {answer: SqlAnswer, df: DataFrame|None, error: str|None, attempts: int}."""
    system = _system("Translate the user's question into one SQLite query.")
    messages = list(history or []) + [{"role": "user", "content": question}]
    for attempt in range(1, MAX_REPAIRS + 2):
        answer = _call(system, messages, SqlAnswer)
        if not answer.sql.strip():
            return {"answer": answer, "df": None, "error": None, "attempts": attempt}
        try:
            return {"answer": answer, "df": db.run_sql(answer.sql), "error": None, "attempts": attempt}
        except Exception as e:  # feed the DB error back and let the model repair its query
            error = str(e)
            messages += [
                {"role": "assistant", "content": answer.model_dump_json()},
                {"role": "user", "content": f"That query failed: {error}. Fix it."},
            ]
    return {"answer": answer, "df": None, "error": error, "attempts": attempt}


def propose_rule(insight: str) -> dict:
    """Turn an insight into a WHERE clause and backtest it. Returns {rule, metrics, error}."""
    system = _system(
        "Turn the FSM's insight into a fraud rule: a single SQL WHERE clause (without the word WHERE) "
        "over the transactions table. Reference other tables only via subqueries, e.g. "
        "card_id IN (SELECT id FROM cards WHERE card_type = 'Debit'). Prefer simple, explainable rules."
    )
    rule = _call(system, [{"role": "user", "content": insight}], RuleProposal)
    try:
        return {"rule": rule, "metrics": db.backtest(rule.where_clause), "error": None}
    except Exception as e:
        return {"rule": rule, "metrics": None, "error": str(e)}