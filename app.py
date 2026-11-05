"""Streamlit UI: explore data in natural language, then turn an insight into a backtested rule."""
import numpy as np
import streamlit as st
import assistant
st.set_page_config(page_title="FSM Assistant", layout="wide")
st.title("FSM Assistant")
explore_tab, rule_tab = st.tabs(["Explore", "Propose rule"])
if "turns" not in st.session_state:
    st.session_state.turns = [] # [{question, result}]
with explore_tab:
    for turn in st.session_state.turns:
        st.chat_message("user").write(turn["question"])
        with st.chat_message("assistant"):
            res = turn["result"]
            st.write(res["answer"].explanation)
            if res["answer"].sql:
                st.code(res["answer"].sql, language="sql")
            if res["answer"].assumptions:
                st.caption("Assumptions: " + " · ".join(res["answer"].assumptions))
            if res["df"] is not None:
                st.dataframe(res["df"], use_container_width=True)
            if res["error"]:
                st.error(f"Query failed after {res['attempts']} attempts: {res['error']}")
	
    if question := st.chat_input("Ask about the transactions, e.g. 'fraud rate by card type'"):
        history = []
        for t in st.session_state.turns[-3:]: # short memory for follow-up questions
            history += [
				{"role": "user", "content": t["question"]},
				{"role": "assistant", "content": t["result"]["answer"].model_dump_json()},
			]
        with st.spinner("Thinking..."):
            result = assistant.ask(question, history)
        st.session_state.turns.append({"question": question, "result": result})
        st.rerun()
        
with rule_tab:
	insight = st.text_area(
		"Describe the pattern you found",
		placeholder="e.g. Swipe transactions with a Bad PIN error at night look risky",
	)
	if st.button("Propose and backtest") and insight:
		with st.spinner("Drafting rule..."):
			out = assistant.propose_rule(insight)
		st.code(out["rule"].where_clause, language="sql")
		st.write(out["rule"].rationale)
		if out["metrics"]:
			cols = st.columns(len(out["metrics"]))
			for col, (k, v) in zip(cols, out["metrics"].items()):
                #Positional formatting avoids scientific notation (e.g. 2.57e-05) for tiny rates.
				col.metric(k, np.format_float_positional(v, trim="-") if isinstance(v, float) else v)
			st.caption("Metrics are computed by SQL over labelled history only. Review before deploying.")
		else:
			st.error(f"Rule did not run: {out['error']}")
