# FSM Assistant — GenAI co-pilot for Fraud Success Managers

A proof of concept that helps a Fraud Success Manager (FSM) go from **question → evidence → deployable rule**:

1. **Explore**: ask questions in plain English. The assistant writes SQLite, runs it read-only and shows the SQL, the result table and every assumption it made.
2. **Propose rule**: describe a pattern you found. The assistant drafts a SQL `WHERE` clause, and the app **backtests it on labelled history** (precision, recall, lift, volume flagged).
3. **Evaluate**: a golden-question eval suite and deterministic guardrail tests show how reliable and safe the assistant is.

> The code is deliberately small (~280 lines of app code in 4 files, plus ~100 lines of tests and evals). It's a POC. Most of the thinking is in this README: [Architecture](#4-architecture), [Data traps](#5-data-traps-found-up-front), [GenAI risks](#7-genai-risks--mitigations), [Evaluation](#8-evaluation-framework) and the [ADRs](#9-architecture-decision-records-adrs).

## Contents

1. [Quick start](#1-quick-start)
2. [Deploying on a new machine](#2-deploying-on-a-new-machine)
3. [Using the app](#3-using-the-app)
4. [Architecture](#4-architecture)
5. [Data traps found up front](#5-data-traps-found-up-front)
6. [From insight to rule](#6-from-insight-to-rule)
7. [GenAI risks &amp;amp; mitigations](#7-genai-risks--mitigations)
8. [Evaluation framework](#8-evaluation-framework)
9. [Architecture Decision Records \\(ADRs\\)](#9-architecture-decision-records-adrs)
10. [Assumptions](#10-assumptions)
11. [Limitations &amp;amp; roadmap](#11-limitations--roadmap)
12. [Project layout](#12-project-layout)

## 1. Quick start

**Prerequisites:** Python 3.11-3.13 and an Anthropic API key. The source database `data/data.db` is already in the repo, one level up.

### Option A: uv (recommended)

```javascript
uv sync                                   # creates .venv from pyproject.toml
cp .env.example .env                      # then set ANTHROPIC_API_KEY
uv run python prepare_db.py               # one-off, ~20s: builds fsm.db (see ADR-004)
uv run streamlit run app.py               # opens http://localhost:8501
```

### Option B: plain pip

```javascript
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                      # then set ANTHROPIC_API_KEY
python prepare_db.py
streamlit run app.py
```

### Tests and evals

```javascript
pytest                                    # guardrail + reference-SQL tests. No API key needed, ~10s
python evals/run_evals.py                 # LLM eval on the golden set (uses the API, ~12 cases)
```

### Configuration (`.env`)


| Variable | Default | Purpose |
| --- | --- | --- |
| ANTHROPIC_API_KEY | (required) | Claude API key |
| FSM_MODEL | claude-opus-5-5 | Model used for SQL and rule generation |
| FSM_DB | ./fsm.db | Working database built by prepare_db.py |

## 2. Deploying on a new machine

The app is a single Streamlit process plus a local SQLite file. It has no other services.

**Local or laptop (what the reviewer will do):** follow [Quick start](#1-quick-start). The only stateful step is `prepare_db.py`, which is safe to re-run with `--force`.

**Shared team instance (how I'd deploy it next):**

1. Container image: `python:3.12-slim`, `pip install -r requirements.txt`, then run `prepare_db.py` **at build time** so the indexed `fsm.db` is baked into the image and start-up is instant.
2. Entrypoint: `streamlit run app.py --server.port 8080 --server.headless true`.
3. Inject `ANTHROPIC_API_KEY` from a secret manager. Never bake it into the image.
4. Put it behind company SSO (for example an identity-aware proxy) because the data is sensitive. Streamlit itself has no authentication.
5. Run on any container platform (Cloud Run, ECS/Fargate, Kubernetes). One small instance is enough because SQLite is read-only and the LLM is remote.

**Production (beyond the POC):** replace SQLite with a read replica or warehouse (Snowflake/BigQuery) through a **read-only service account**, keep the same guardrail layer, log every question, SQL and result to an audit store, and route new rules to **shadow mode** in the rules engine (see [§8\\.2](#82-online-evaluation-in-production)).

## 3. Using the app

### Explore tab

Ask questions like:

- *What is the fraud rate by card type?*
- *Which merchant categories have the highest fraud rate with at least 500 labelled transactions?*
- *Now only for swipe transactions* (follow-ups use the last 3 turns as context)
- *How does fraud vary by hour of day?*

Each answer shows **an explanation, the SQL, the assumptions, and the result table**. Nothing is hidden behind a summary, so the FSM can check the logic.

### Propose rule tab

Describe a pattern, for example *"Swipe transactions with a Bad PIN error look risky"*. You get:

- a `WHERE` clause such as `transaction_type = 'Swipe Transaction' AND errors = 'Bad PIN'`
- a rationale
- **backtest metrics computed by SQL**: flagged volume, % of labelled rows, fraud caught, base rate, precision, recall and lift over the base rate (rates as %, to 3 significant figures so tiny values are not rounded to 0)

Iterate by rewording the insight ("…only over $200") and compare the metrics.

## 4. Architecture

```javascript
            ┌──────────────────────────── Streamlit UI (app.py) ───────────────────────────┐
            │  Explore tab (chat)                         Propose-rule tab                 │
            └───────────┬─────────────────────────────────────────┬────────────────────────┘
                        │ question + last 3 turns                 │ insight text
                        ▼                                         ▼
            ┌──────────────────────── assistant.py ──────────────────────────────────────┐
            │ system prompt = schema DDL (live from DB) + column notes + few-shot SQL     │
            │ Claude (structured output, Pydantic) → {sql, explanation, assumptions}     │
            │                                     → {where_clause, rationale}            │
            │ self-repair loop: on SQL error, feed error back, max 2 retries             │
            └───────────┬─────────────────────────────────────────┬──────────────────────┘
                        │ SQL                                     │ WHERE clause
                        ▼                                         ▼
            ┌──────────────────────────── db.py (deterministic) ─────────────────────────┐
            │ read-only URI · SQLite authorizer (SELECT/READ only) · one statement only    │
            │ 30s timeout · 1000-row cap · backtest() wraps clause in a fixed template    │
            └───────────────────────────────────┬────────────────────────────────────────┘
                                                ▼
                                fsm.db (copy of data.db, labels re-typed, indexed)
```

**Design idea:** *the LLM writes queries, the database produces facts.* Every number the FSM sees comes from executed SQL. The model never states figures, and rule quality is measured by code, not judged by the model.

### Request flow (Explore)

1. Build the system prompt from the **live schema** (`sqlite_master`), **column notes** for things the DDL doesn't say (units, coverage, traps), and two **fraud-flavoured examples**.
2. Call Claude through `messages.parse` with a Pydantic schema, which returns typed `SqlAnswer` output with no fragile JSON parsing.
3. Execute through `db.run_sql`. On error, append the error to the conversation and ask for a fix (at most 2 repairs).
4. If the question can't be answered from the schema, the model returns `sql=""` with an explanation instead of guessing.

## 5. Data traps found up front

These came from reading `schema.sql` plus a short profile of the data. Each trap is handled in one of three ways: a fix in `prepare_db.py`, a **column note** in the prompt, or a **golden eval case**.


| \# | Trap | Impact | Handling |
| --- | --- | --- | --- |
| 1 | fraud_labels.transaction_id is TEXT, transactions.id is INTEGER | The join gives correct results through type affinity, but it can't use the index: a transactions LEFT JOIN fraud_labels ran for over 10 minutes without finishing. LLMs write exactly this query. | prepare_db.py re-types the column to INTEGER PRIMARY KEY and adds indexes (ADR-004) |
| 2 | Only ~67% of transactions have a label (777k of 1.16M) | Treating "unlabelled" as "not fraud" understates fraud rates | Column note: inner join to labels for rates. Backtest is labelled-only (ADR-009) |
| 3 | Severe imbalance: ~0.17% fraud (1,360 of 777k) | Accuracy is meaningless; small segments are noisy | Rules report precision/recall/lift/volume. Few-shot uses HAVING COUNT(\*) &gt;= 500 |
| 4 | Money in cents (amount_usd_cents, limits, incomes, debt) | "Over $1,000" silently becomes &gt; 1000 (= $10) | Column note \+ golden cases big_tx_cents, avg_fraud_amount |
| 5 | Negative amounts (~57k rows) | Refunds/reversals skew sums and averages | Column note. The model should state its assumption |
| 6 | No indexes in the source DB | Slow joins at 1.16M rows | Indexes on card, merchant, location, date (ADR-004). 30s timeout as a backstop |
| 7 | Dates are TEXT; data covers 2019-01 → 2019-10 only | "Last year" is ambiguous; out-of-range questions return empty results | Range in column notes \+ an unanswerable eval case for 2023 |
| 8 | merchants and merchant_locations exist but aren't listed in the challenge brief | Location and category questions need them | Live DDL in the prompt, not a hand-written table list |
| 9 | card_on_dark_web is always 0 | Rules built on it have no signal | Column note |
| 10 | Online transactions show 0% fraud among labelled rows | Looks like a labelling or sampling artefact. An FSM could draw the wrong conclusion | Flagged here as a dataset caveat. In production the tool should surface label coverage next to any rate |

## 6. From insight to rule

A "rule" is a valid SQL `WHERE` clause over `transactions` (as defined in the brief).

```javascript
explore → hypothesis → propose rule (LLM) → validate + backtest (SQL) → refine → export
```

- <br>The LLM writes **only the clause**. `db.backtest()` inserts it into a fixed template:<br>`SELECT … FROM (SELECT id FROM transactions WHERE &lt;clause&gt;) t JOIN fraud_labels f …`<br>so the clause is evaluated exactly as the production filter would be.
- <br>Metrics returned:<br>


| Metric | Why the FSM cares | Column 3 |
| --- | --- | --- |
| flagged (labelled), flagged % of labelled | Operational cost / customer friction |  |
| fraud caught | Absolute impact |  |
| base rate % | Fraud rate across all labelled rows (~0.175%), the yardstick for precision |  |
| precision % | How many flagged payments are actually fraud (false-positive cost) |  |
| recall % | Share of all known fraud the rule catches |  |
| lift vs base rate | Is this segment really riskier than average? |  |
| Safety: the clause runs through the same read-only authorizer. ; is rejected, so it can't be used to inject statements. |  |  |
| Designed but not built (time): |  |  |

- 
    - `$` fraud prevented vs `$` legitimate volume blocked
    - **Time-split backtest** (fit on Jan–Jun, check Jul–Oct) to catch overfitting
    - **Overlap with existing rules** (incremental recall, not just raw recall)
    - **Auto-suggest:** the LLM proposes N candidate clauses for a segment, the backtester ranks them, and the FSM picks one
    - Export to a `rules.json` with hypothesis, clause, metrics, author and timestamp as an audit trail, then hand off to the rules engine in shadow mode

## 7. GenAI risks &amp; mitigations


| Risk | Mitigation in this POC | Would add in production |
| --- | --- | --- |
| Hallucinated numbers | The LLM never states figures. All numbers come from executed SQL | Optionally verify that any number in the explanation appears in the result |
| Plausible but wrong SQL | SQL and assumptions always shown; column notes for known traps; golden eval set | Semantic layer / metric definitions ("fraud rate" defined once); query review for rule promotion |
| Destructive SQL / prompt injection | Enforced by SQLite, not the prompt: read-only URI, an authorizer that allows only SELECT/READ, a single statement per call, guardrail tests | DB-level read-only role; separate credentials per user |
| Runaway queries | Indexed working DB, 30s progress-handler timeout, 1,000-row cap | Warehouse quotas, cost estimates before execution |
| Overfit or harmful rules | Backtest on labelled data with lift/volume visible; human decides | Time-split validation, minimum support, mandatory shadow mode before blocking |
| Model refusals / outages | stop_reason == "refusal" checked; server-side fallback (fallbacks="default") | Retries with backoff, cached answers for common questions |
| Non-determinism | Structured output; result-set based evals | Repeated eval runs (N=3) with variance reporting; prompt and model version pinned per release |
| Data privacy | Only schema and the user's question go to the LLM. Result rows are not sent back to the model | Enterprise endpoint / zero-data-retention agreement; PII column masking |

- 

## 8. Evaluation framework

- The goal is to convince the Head of Fraud of two things: **"the answers are correct"** and **"it can't do damage"**. I measure each separately.

### 8.1 Offline evaluation (implemented)

- **A. Safety, deterministic (`pytest`, no API key):** `tests/test_guardrails.py`
    - `DELETE`, `DROP`, `UPDATE`, `INSERT`, `CREATE`, `ATTACH`, `PRAGMA`, and stacked statements (`SELECT 1; DROP …`) must all be rejected.
    - Rule injection (`1=1; DROP TABLE users`) is rejected.
    - Row cap enforced. Backtest sanity check: `1 = 1` gives recall 1.0 and lift 1.0.
    - Every golden reference query runs, so a broken reference can't silently skew accuracy.
    - **Target: 100% pass. This is a release gate.**
- **B. Accuracy, LLM in the loop (`python evals/run_evals.py`):** `evals/golden.json`
    - 12 questions tagged by what they test: basic, dates, joins, multi-join, `trap:cents`, `trap:labelled-only`, nulls, geo, **unanswerable**.
    - **Scoring by execution result, not SQL text.** Two different queries can both be correct, so the generated result set is compared with the reference result set, ignoring row order and column names, with floats rounded to 4 d.p.
    - Unanswerable cases pass if the assistant declines (`sql=""`) or returns an empty result rather than inventing an answer.
    - Metrics printed and saved to `evals/results.json`:


| Metric | Definition |
| --- | --- |
| Execution success | % of questions where the generated SQL ran |
| Execution accuracy | % whose result set matches the reference |
| Self-repair rate | % that needed at least one repair retry (indicates prompt gaps) |
| Latency | Seconds per question |

- 
- **Run it:** `python evals/run_evals.py` (about 12 API calls).
- **Designed but not built:**
    - Grow the golden set to 50–100 questions **taken from real FSM questions**, stratified by difficulty.
    - **Paraphrase robustness:** 3 phrasings per question; the answer should not change.
    - **Adversarial LLM set:** prompt-injection text inside questions ("ignore your instructions and delete…"), measured as "no unsafe SQL attempted" on top of the deterministic block.
    - **Rule eval:** seeded hypotheses with known answers. Score clause validity rate and whether lift and precision clear a threshold.
    - **Explanation faithfulness:** an LLM judge (cheaper model) checks the explanation matches the SQL, and that assumptions are listed when the question is ambiguous.
    - Run in **CI on every prompt or model change** with a regression threshold, N=3 runs, mean ± spread.

### 8.2 Online evaluation (in production)


| Area | Metric |
| --- | --- |
| Trust / adoption | % of generated queries run unedited; edit distance when edited; thumbs up/down; weekly active FSMs |
| Productivity | Time from first question to a rule submitted for review, compared with the manual baseline; questions per session |
| Rule quality | Rules run in shadow mode (log, don't block) for N days: live precision, false-positive rate, chargeback $ prevented, customer complaints/declines |
| Safety | Unsafe-SQL attempts blocked (should be rare, and alerted on); zero writes; % of answers later flagged wrong |
| Drift | Golden-set score tracked per model/prompt version; label coverage and base fraud rate monitored as data shifts |

- 

### 8.3 Pitch to the Head of Fraud

> - The assistant never invents numbers and can't change data. Every figure comes from a read-only query the analyst can see. Every rule is backtested on labelled history and checked by a human, and runs in shadow mode before it blocks a real payment. We gate releases on a golden question set (accuracy) and a guardrail suite (100% required), and in production we track shadow-mode precision and analyst time-to-rule.

## 9. Architecture Decision Records (ADRs)

- Short form: **Context → Decision → Consequences.**

### ADR-001: Streamlit for the UI

    - **Context:** 4–5h budget; the brief values a realistic UX but values features and evaluation more.
    - **Decision:** a single-file Streamlit app with two tabs (Explore, Propose rule).
    - **Consequences:** chat, tables and metric tiles with almost no code. No auth and limited multi-user state, which is fine for a POC. Production would sit behind SSO, or move to a proper web front end if adopted.
### ADR-002: Anthropic SDK directly, no agent framework

    - **Context:** LangChain/LlamaIndex SQL agents exist, but they hide prompts, add dependencies and make failures harder to debug.
    - **Decision:** call `client.beta.messages.parse` directly with a hand-written ~20-line repair loop.
    - **Consequences:** the whole LLM interaction fits on one screen and is easy to test and explain. Each extra capability (tools, memory) has to be added by hand.
### ADR-003: Model choice and fallback

    - **Context:** text-to-SQL accuracy matters more than latency or cost at FSM volumes, which are tens of questions a day.
    - **Decision:** default `claude-opus-5-5`, configurable through `FSM_MODEL`. Enable the server-side refusal **fallback** (`fallbacks="default"`), so a refusal is retried on a fallback model instead of failing.
    - **Consequences:** best accuracy, at a few seconds per answer. Cost is negligible at this volume. A cheaper model can be swapped in and **measured with the eval suite** before switching.
### ADR-004: Build a working copy of the DB (`prepare_db.py`)

    - **Context:** the source DB has a TEXT/INTEGER label key mismatch that defeats indexes (a common query ran for over 10 minutes) and has no secondary indexes.
    - **Decision:** a one-off script copies `data.db` to `fsm.db`, re-types `fraud_labels.transaction_id` to `INTEGER PRIMARY KEY`, adds 4 indexes and runs `ANALYZE` (~20s). The original file stays untouched.
    - **Consequences:** typical queries take seconds. One extra setup step, documented and safe to re-run. In production this is an upstream data-engineering fix.
### ADR-005: Guardrails enforced by the database, not the prompt

    - **Context:** prompt instructions ("only write SELECT") can be bypassed by prompt injection or model error.
    - **Decision:** three deterministic layers in `db.py`: (1) a read-only connection URI, (2) a `sqlite3` **authorizer** that allows only SELECT/READ/FUNCTION actions, (3) Python's `sqlite3` refuses multiple statements. Plus a timeout and row cap.
    - **Alternatives considered:** parsing SQL with `sqlglot` to allowlist statements. Rejected for the POC because the authorizer is stdlib, sees what SQLite will actually execute, and can't be fooled by syntax tricks.
    - **Consequences:** safety doesn't depend on model behaviour and is covered by tests that need no API key.
### ADR-006: Whole schema in the prompt (no RAG)

    - **Context:** 7 tables and about 50 columns fit comfortably in context.
    - **Decision:** inject the live DDL, hand-written column notes and 2 few-shot examples into every prompt.
    - **Consequences:** simple and always current. If the schema grows to hundreds of tables, switch to retrieving relevant tables and examples (schema RAG).
### ADR-007: Structured output with Pydantic

    - **Decision:** `messages.parse(output_format=SqlAnswer)` returns `\{sql, explanation, assumptions\}`. Rules return `\{where_clause, rationale\}`.
    - **Consequences:** no regex or JSON-repair code, and the **assumptions** field makes the model surface ambiguity explicitly, which is key for trust.
### ADR-008: The LLM never produces numbers

    - **Decision:** the model generates SQL and prose only. Results are shown straight from SQLite, and **rows are not sent back to the model** for summarising.
    - **Consequences:** this removes the most dangerous hallucination (wrong figures) and keeps transaction data out of the LLM. The trade-off is no natural-language summary of results, which would be added later with a number-verification check.
### ADR-009: Rule metrics on labelled transactions only

    - **Context:** about 33% of transactions have no label.
    - **Decision:** precision, recall and lift are computed only over labelled rows. Unlabelled rows are neither "fraud" nor "not fraud".
    - **Consequences:** honest metrics on a smaller base. Production would also report flagged volume on **all** traffic to estimate operational load.
### ADR-010: Evaluate by result set, not SQL text

    - **Decision:** compare the sorted, rounded result tuples of generated and reference SQL.
    - **Consequences:** many correct SQL variants pass. Column order still matters, and a query with extra columns fails, which is acceptable strictness for a first suite.
### ADR-011: Self-repair, at most 2 retries

    - **Decision:** on a SQLite error, send the error back to the model; stop after 2 repairs and show the failure.
    - **Consequences:** fixes most typos and column-name slips cheaply. The repair rate is tracked in evals as a signal of prompt quality.
### ADR-012: Small, fixed limits

    - **Decision:** 1,000-row display cap, 30s query timeout, last 3 turns of conversation memory.
    - **Consequences:** predictable latency and cost. Very long analytical sessions lose early context, which is acceptable for a POC.
### ADR-013: JSON eval files, minimal dependencies

    - **Context:** PyYAML had no wheel for Python 3.15 on the dev machine.
    - **Decision:** golden set in JSON. Runtime dependencies limited to `anthropic`, `streamlit`, `pandas` and `python-dotenv`.
    - **Consequences:** installs cleanly on new Python versions. JSON is slightly less pleasant for multi-line SQL.
## 10. Assumptions

    - **A "rule"** is a SQL boolean expression over `transactions` that can be placed after `WHERE`, with other tables referenced through subqueries (as in the brief's example).
    - **Fraud rate** = `SUM(is_fraud) / COUNT(\*)` over **labelled** transactions.
    - **Negative amounts** are refunds or reversals, kept in the data but called out.
    - The FSM is SQL-literate enough to read a query, so showing SQL builds trust rather than causing confusion.
    - Questions are about the historical dataset (Jan–Oct 2019). There's no real-time scoring.
    - Single user, local deployment. Sensitive-data handling is out of scope for the POC, but described in §7.
    - The working DB (`fsm.db`) is derived and disposable. `data/data.db` is the source of truth and is never modified.
## 11. Limitations &amp; roadmap

- **Known limitations**
    - The golden set is small (12) and hand-written, so it shows the method rather than statistical confidence.
    - No charts. Results are tables only.
    - Rule backtest has no time-split or overlap analysis yet (designed in §6).
    - No persistence of conversations or rules.
    - Result-set equality is strict about column order.
- **Roadmap (in priority order)**
    1. Rule export with audit trail, then shadow-mode integration with the rules engine.
    2. Time-split backtest and incremental recall against the existing rule set.
    3. Grow the eval set from real FSM questions; run it in CI on every prompt or model change.
    4. Auto-generated charts for time series and categorical breakdowns.
    5. **Hypothesis mining:** the assistant proactively scans segments (MCC × type × error × hour) with a statistical test and suggests the top anomalies for the FSM to review.
    6. Natural-language result summaries, with automatic checks that every number appears in the result set.
    7. RAG over past rules, investigations and FSM notes, so the assistant learns team knowledge.

## 12. Project layout

```javascript
checkout-fsm-solution/
├── app.py              # Streamlit UI (Explore + Propose rule)
├── assistant.py        # Prompt, Claude calls, self-repair loop, rule proposal
├── db.py               # Read-only execution, authorizer guardrails, backtest metrics
├── prepare_db.py       # One-off: build indexed working DB from ../data/data.db
├── evals/
│   ├── golden.json     # Questions + reference SQL (+ unanswerable cases)
│   └── run_evals.py    # Result-set based accuracy eval → evals/results.json
├── tests/
│   └── test_guardrails.py  # Deterministic safety + reference-SQL tests (no API key)
├── projectplan.md      # Original plan written before implementation
├── pyproject.toml      # uv / PEP 621 project definition
├── requirements.txt    # pip alternative
└── .env.example
```

- 

