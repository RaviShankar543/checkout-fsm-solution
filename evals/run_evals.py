"""Offline eval: run golden questions through the assistant and compare result sets (not SQL text).

Usage: python evals/run_evals.py  (needs ANTHROPIC_API_KEY; calls the LLM once or more per case)
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
import assistant # noqa: E402
import db # noqa: E402


def normalise(df) -> list:
	"""Order-insensitive, column-name-insensitive, float-tolerant comparison key."""
	rows = [tuple(round(v, 4) if isinstance(v, float) else v for v in row) for row in df.itertuples(index=False)]
	return sorted(rows, key=str)


def main():
	cases = json.loads((Path(__file__).parent / "golden.json").read_text())
	results = []
	for case in cases:
		t0 = time.time()
		try:
			out = assistant.ask(case["question"])
			if case["sql"] is None: # expect the assistant to decline (or return nothing)
				passed = not out["answer"].sql.strip() or (out["df"] is not None and out["df"].dropna(how="all").empty)
				executed = True
			else:
				executed = out["df"] is not None
				passed = executed and normalise(out["df"]) == normalise(db.run_sql(case["sql"]))
			note = out["answer"].sql
		except Exception as e:
			executed, passed, note, out = False, False, f"ERROR {e}", {"attempts": 0}
		results.append({"id": case["id"], "tags": case["tags"], "executed": executed, "passed": passed,
						"attempts": out["attempts"], "seconds": round(time.time() - t0, 1), "sql": note})
		print(f"{'PASS' if passed else 'FAIL'} {case['id']:<26} attempts={out['attempts']} {results[-1]['seconds']}s")
		
	n = len(results)
	print(f"\nExecution success: {sum(r['executed'] for r in results)}/{n}")
	print(f"Execution accuracy: {sum(r['passed'] for r in results)}/{n}")
	print(f"Needed self-repair: {sum(r['attempts'] > 1 for r in results)}/{n}")
	(Path(__file__).parent / "results.json").write_text(json.dumps(results, indent=2))


if __name__ == "__main__":
	main()