import uuid
import hashlib

import streamlit as st

from log_parser import prepare_log
from graph import build_graph, start_investigation, resume_investigation, is_waiting_for_approval
from llm import chat_claude

st.set_page_config(page_title="AI Incident Investigator", page_icon="🔍", layout="wide")
st.title("🔍 AI Incident Investigator")
st.caption("Upload a log file, let the agents investigate, approve the fix, then chat about it.")


# ---------- Session state (Streamlit reruns this file on every click) ----------
if "graph" not in st.session_state:
    st.session_state.graph = build_graph()


def reset_for_new_log(file_id: str, text: str):
    st.session_state.file_id = file_id
    st.session_state.log = prepare_log(text)
    st.session_state.thread_id = str(uuid.uuid4())  # new investigation
    st.session_state.result = None
    st.session_state.messages = []                   # new chat


# ---------- 1. Upload ----------
uploaded = st.file_uploader("Upload a log file", type=["log", "txt"])
if uploaded is None:
    st.info("Upload a log file to begin. Try sample_logs/sample_app.log.")
    st.stop()

text = uploaded.getvalue().decode("utf-8", errors="replace")
file_id = hashlib.md5(text.encode()).hexdigest()
if st.session_state.get("file_id") != file_id:
    reset_for_new_log(file_id, text)

log = st.session_state.log
summary = log["summary"]
counts = summary["level_counts"]

c1, c2, c3, c4 = st.columns(4)
c1.metric("Total lines", summary["total_lines"])
c2.metric("Errors", counts.get("ERROR", 0))
c3.metric("Warnings", counts.get("WARN", 0) + counts.get("WARNING", 0))
c4.metric("Services with errors", len(summary["errors_by_service"]))

with st.expander("Errors and warnings sent to Claude"):
    st.code(log["llm_text"], language="text")


# ---------- 2. Investigation ----------
st.header("Investigation")
graph = st.session_state.graph
thread_id = st.session_state.thread_id
result = st.session_state.result

if result is None:
    if st.button("Run investigation", type="primary"):
        try:
            with st.spinner("Analyzing logs, finding the root cause, drafting a fix..."):
                st.session_state.result = start_investigation(graph, log["llm_text"], thread_id)
            st.rerun()
        except Exception as e:
            st.error(f"Something went wrong: {e}")
else:
    with st.expander("1. What happened", expanded=True):
        st.markdown(result["analysis"])
    with st.expander("2. Root cause", expanded=True):
        st.markdown(result["diagnosis"])

    if is_waiting_for_approval(graph, thread_id):
        # The graph is paused at interrupt() waiting for you
        st.subheader("3. Proposed fix: waiting for your approval")
        st.markdown(result["fix_plan"])
        feedback = st.text_area("Feedback (add this if you want a revised plan)")

        col_a, col_r = st.columns(2)
        if col_a.button("✅ Approve", type="primary"):
            with st.spinner("Writing the incident report..."):
                st.session_state.result = resume_investigation(graph, thread_id, approved=True)
            st.rerun()
        if col_r.button("❌ Reject"):
            with st.spinner("Revising the plan..."):
                st.session_state.result = resume_investigation(
                    graph, thread_id, approved=False, feedback=feedback.strip()
                )
            st.rerun()

    elif result.get("report"):
        with st.expander("3. Approved fix"):
            st.markdown(result["fix_plan"])
        st.subheader("4. Incident report")
        st.markdown(result["report"])
        st.download_button("Download report", result["report"], file_name="incident_report.md")

    else:
        st.warning("The fix was rejected without feedback, so the investigation stopped.")
        if st.button("Start over"):
            st.session_state.thread_id = str(uuid.uuid4())
            st.session_state.result = None
            st.rerun()


# ---------- 3. Chat ----------
st.header("💬 Chat about this log")


def build_system_prompt() -> str:
    parts = [
        "You are an expert assistant helping a user understand an application log.",
        "Answer using ONLY the log data and investigation below. Cite line numbers like (L13).",
        "If the answer is not in the data, say so instead of guessing. Keep answers short.",
        "",
        "LOG DATA:",
        log["llm_text"],
    ]
    res = st.session_state.result
    if res:
        parts += [
            "",
            "INVESTIGATION RESULTS:",
            f"Analysis: {res.get('analysis', '')}",
            f"Diagnosis: {res.get('diagnosis', '')}",
            f"Fix plan: {res.get('fix_plan', '')}",
        ]
    return "\n".join(parts)


# Show previous messages
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

if question := st.chat_input("Ask about the logs, e.g. 'Why did payments fail?'"):
    st.session_state.messages.append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.markdown(question)

    with st.chat_message("assistant"):
        try:
            with st.spinner("Thinking..."):
                # Last 9 messages (odd number, so the history starts with a user message)
                answer = chat_claude(st.session_state.messages[-9:], system=build_system_prompt())
            st.markdown(answer)
            st.session_state.messages.append({"role": "assistant", "content": answer})
        except Exception as e:
            st.session_state.messages.pop()  # remove the unanswered question
            st.error(f"Something went wrong: {e}")