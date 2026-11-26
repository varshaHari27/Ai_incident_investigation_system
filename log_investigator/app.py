import uuid
import hashlib

import streamlit as st

from log_parser import prepare_log
from graph import (
    build_graph, start_investigation, resume_investigation,
    get_investigation, get_status, list_incidents,
)
from llm import chat_claude

st.set_page_config(page_title="AI Incident Investigator", page_icon="🔍", layout="wide")


@st.cache_resource
def get_graph():
    # One graph for the whole app; its state lives in incidents.db
    return build_graph()


graph = get_graph()

STATUS_LABELS = {
    "investigating": "⏳ Investigating",
    "waiting_for_approval": "🟠 Waiting for approval",
    "resolved": "✅ Resolved",
    "stopped": "⛔ Stopped",
}


# ---------- Shared: show one investigation (used by both modes) ----------
def show_investigation(thread_id: str) -> dict:
    result = get_investigation(graph, thread_id)
    status = get_status(graph, thread_id)

    if status == "investigating":
        st.info("⏳ The investigation is still running. Click Refresh in a moment.")
        return result

    with st.expander("1. What happened", expanded=True):
        st.markdown(result.get("analysis", ""))
    with st.expander("2. Root cause", expanded=True):
        st.markdown(result.get("diagnosis", ""))

    if status == "waiting_for_approval":
        st.subheader("3. Proposed fix: waiting for your approval")
        st.markdown(result["fix_plan"])
        feedback = st.text_area("Feedback (add this if you want a revised plan)", key=f"fb_{thread_id}")

        col_a, col_r = st.columns(2)
        if col_a.button("✅ Approve", type="primary", key=f"approve_{thread_id}"):
            with st.spinner("Writing the incident report..."):
                resume_investigation(graph, thread_id, approved=True)
            st.rerun()
        if col_r.button("❌ Reject", key=f"reject_{thread_id}"):
            with st.spinner("Revising the plan..."):
                resume_investigation(graph, thread_id, approved=False, feedback=feedback.strip())
            st.rerun()

    elif status == "resolved":
        with st.expander("3. Approved fix"):
            st.markdown(result["fix_plan"])
        st.subheader("4. Incident report")
        st.markdown(result["report"])
        st.download_button("Download report", result["report"],
                           file_name=f"incident_{thread_id[:8]}.md", key=f"dl_{thread_id}")

    else:  # stopped
        st.warning("The fix was rejected without feedback, so this investigation stopped.")

    return result


# ---------- Shared: chat about one log (used by both modes) ----------
def build_system_prompt(log_text: str, result: dict) -> str:
    parts = [
        "You are an expert assistant helping a user understand an application log.",
        "Answer using ONLY the log data and investigation below. Cite line numbers like (L13).",
        "If the answer is not in the data, say so instead of guessing. Keep answers short.",
        "",
        "LOG DATA:",
        log_text,
    ]
    if result.get("analysis"):
        parts += [
            "",
            "INVESTIGATION RESULTS:",
            f"Analysis: {result.get('analysis', '')}",
            f"Diagnosis: {result.get('diagnosis', '')}",
            f"Fix plan: {result.get('fix_plan', '')}",
        ]
    return "\n".join(parts)


def show_chat(thread_id: str, log_text: str, result: dict):
    st.header("💬 Chat about this log")
    chats = st.session_state.setdefault("chats", {})
    messages = chats.setdefault(thread_id, [])  # separate chat per incident

    for msg in messages:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    if question := st.chat_input("Ask about the logs, e.g. 'Why did payments fail?'"):
        messages.append({"role": "user", "content": question})
        with st.chat_message("user"):
            st.markdown(question)

        with st.chat_message("assistant"):
            try:
                with st.spinner("Thinking..."):
                    # Last 9 messages (odd number, so the history starts with a user message)
                    answer = chat_claude(messages[-9:], system=build_system_prompt(log_text, result))
                st.markdown(answer)
                messages.append({"role": "assistant", "content": answer})
            except Exception as e:
                messages.pop()  # remove the unanswered question
                st.error(f"Something went wrong: {e}")


# ---------- Mode 1: Live incidents (from the watcher) ----------
def live_page():
    st.subheader("🚨 Incidents")
    st.caption("Detected automatically by watcher.py, plus any uploads you investigated.")
    if st.button("🔄 Refresh"):
        st.rerun()

    incidents = list_incidents()
    if not incidents:
        st.info("No incidents yet. Run watcher.py and fake_app.py to create one.")
        return

    status_of = {inc["thread_id"]: get_status(graph, inc["thread_id"]) for inc in incidents}
    labels = {
        inc["thread_id"]: f"{STATUS_LABELS[status_of[inc['thread_id']]]} · {inc['title']} · "
                          f"{inc['created_at']} ({inc['source']})"
        for inc in incidents
    }

    waiting = sum(1 for s in status_of.values() if s == "waiting_for_approval")
    if waiting:
        st.warning(f"{waiting} incident(s) waiting for your approval")

    thread_id = st.selectbox("Choose an incident", list(labels), format_func=labels.get)
    st.divider()

    result = show_investigation(thread_id)
    if result.get("log_text"):
        with st.expander("Logs that were sent to Claude"):
            st.code(result["log_text"], language="text")
        show_chat(thread_id, result["log_text"], result)


# ---------- Mode 2: Upload a log file (same as before) ----------
def upload_page():
    uploaded = st.file_uploader("Upload a log file", type=["log", "txt"])
    if uploaded is None:
        st.info("Upload a log file to begin. Try sample_logs/sample_app.log.")
        return

    text = uploaded.getvalue().decode("utf-8", errors="replace")
    file_id = hashlib.md5(text.encode()).hexdigest()
    if st.session_state.get("upload_file_id") != file_id:  # new file: fresh investigation
        st.session_state.upload_file_id = file_id
        st.session_state.upload_log = prepare_log(text)
        st.session_state.upload_thread = str(uuid.uuid4())
        st.session_state.upload_started = False

    log = st.session_state.upload_log
    thread_id = st.session_state.upload_thread
    counts = log["summary"]["level_counts"]

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total lines", log["summary"]["total_lines"])
    c2.metric("Errors", counts.get("ERROR", 0))
    c3.metric("Warnings", counts.get("WARN", 0) + counts.get("WARNING", 0))
    c4.metric("Services with errors", len(log["summary"]["errors_by_service"]))

    with st.expander("Errors and warnings sent to Claude"):
        st.code(log["llm_text"], language="text")

    st.header("Investigation")
    result = {}
    if not st.session_state.upload_started:
        if st.button("Run investigation", type="primary"):
            try:
                with st.spinner("Analyzing logs, finding the root cause, drafting a fix..."):
                    start_investigation(graph, log["llm_text"], thread_id,
                                        title=f"Upload: {uploaded.name}", source="upload")
                st.session_state.upload_started = True
                st.rerun()
            except Exception as e:
                st.error(f"Something went wrong: {e}")
    else:
        result = show_investigation(thread_id)

    show_chat(thread_id, log["llm_text"], result)


# ---------- Page layout ----------
st.title("🔍 AI Incident Investigator")
mode = st.sidebar.radio("Mode", ["🚨 Live incidents", "📄 Upload a log"])

if mode == "🚨 Live incidents":
    live_page()
else:
    upload_page()