import sqlite3
from datetime import datetime
from typing import TypedDict

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import interrupt, Command

from llm import ask_claude

DB_PATH = "incidents.db"  # shared by the watcher and the Streamlit app


# ---------- 1. Shared state: every step reads from and writes to this ----------
class IncidentState(TypedDict, total=False):
    log_text: str    # output of log_parser.prepare_log()["llm_text"]
    analysis: str    # what happened
    diagnosis: str   # why it happened (root cause)
    fix_plan: str    # how to fix it
    approved: bool   # your decision
    feedback: str    # your comments if you reject
    report: str      # final incident report


# ---------- 2. The steps (nodes) ----------
def analyze(state: IncidentState) -> dict:
    prompt = f"""Here is a summary of an application log with its important lines:

{state["log_text"]}

Describe what happened: which services were affected, when problems started,
and a short timeline of the key events. Cite line numbers like (L13).
Do not guess the root cause yet. Keep it under 200 words."""
    return {"analysis": ask_claude(prompt)}


def diagnose(state: IncidentState) -> dict:
    prompt = f"""Log data:
{state["log_text"]}

Analysis of what happened:
{state["analysis"]}

Find the most likely ROOT CAUSE (the first thing that went wrong, not just the symptoms).
- Explain how the errors are connected (what caused what).
- Cite the exact line numbers that prove it, like (L8).
- Give a confidence level: High, Medium, or Low.
- If the logs don't contain enough evidence, say so clearly instead of guessing.
Keep it under 250 words."""
    return {"diagnosis": ask_claude(prompt)}


def suggest_fix(state: IncidentState) -> dict:
    feedback_note = ""
    if state.get("feedback"):
        feedback_note = f"""
A reviewer REJECTED your previous plan with this feedback:
"{state["feedback"]}"
Revise the plan to address it.
"""
    prompt = f"""Root cause diagnosis:
{state["diagnosis"]}
{feedback_note}
Suggest a remediation plan with:
1. Immediate fix (to stop the problem now)
2. Rollback option (if the fix doesn't work)
3. Prevention (so it doesn't happen again)
Keep each part short and practical."""
    return {"fix_plan": ask_claude(prompt)}


def approval(state: IncidentState) -> dict:
    # The graph PAUSES here and its state is saved to incidents.db.
    # It resumes when someone calls resume_investigation() with a decision,
    # even from a different program (like the Streamlit app).
    decision = interrupt({"fix_plan": state["fix_plan"]})
    return {
        "approved": decision["approved"],
        "feedback": decision.get("feedback", ""),
    }


def final_report(state: IncidentState) -> dict:
    prompt = f"""Write a short incident report in Markdown with these sections:
## Summary
## Timeline
## Root Cause
## Approved Fix
## Prevention

Use this information:
ANALYSIS: {state["analysis"]}
DIAGNOSIS: {state["diagnosis"]}
APPROVED FIX PLAN: {state["fix_plan"]}"""
    return {"report": ask_claude(prompt, max_tokens=2000)}


# ---------- 3. Decide where to go after approval ----------
def after_approval(state: IncidentState) -> str:
    if state["approved"]:
        return "final_report"
    if state.get("feedback"):
        return "suggest_fix"  # rejected with feedback: try again
    return END                # rejected with no feedback: stop


# ---------- 4. Small incidents table (so the app can list investigations) ----------
def _run_sql(query: str, params: tuple = ()) -> list:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    try:
        rows = conn.execute(query, params).fetchall()
        conn.commit()
        return rows
    finally:
        conn.close()


def register_incident(thread_id: str, title: str, source: str):
    _run_sql(
        "INSERT OR IGNORE INTO incidents (thread_id, title, source, created_at) VALUES (?, ?, ?, ?)",
        (thread_id, title, source, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    )


def list_incidents() -> list[dict]:
    """All investigations, newest first."""
    rows = _run_sql("SELECT thread_id, title, source, created_at FROM incidents ORDER BY created_at DESC")
    return [dict(zip(["thread_id", "title", "source", "created_at"], row)) for row in rows]


# ---------- 5. Connect the steps into a graph ----------
def build_graph():
    builder = StateGraph(IncidentState)

    builder.add_node("analyze", analyze)
    builder.add_node("diagnose", diagnose)
    builder.add_node("suggest_fix", suggest_fix)
    builder.add_node("approval", approval)
    builder.add_node("final_report", final_report)

    builder.add_edge(START, "analyze")
    builder.add_edge("analyze", "diagnose")
    builder.add_edge("diagnose", "suggest_fix")
    builder.add_edge("suggest_fix", "approval")
    builder.add_conditional_edges("approval", after_approval, ["final_report", "suggest_fix", END])
    builder.add_edge("final_report", END)

    # Save state to a SQLite file so other programs can see and resume it
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")  # lets two programs use the file at once
    conn.execute(
        "CREATE TABLE IF NOT EXISTS incidents "
        "(thread_id TEXT PRIMARY KEY, title TEXT, source TEXT, created_at TEXT)"
    )
    conn.commit()

    return builder.compile(checkpointer=SqliteSaver(conn))


# ---------- 6. Helpers used by the app and the watcher ----------
def _config(thread_id: str) -> dict:
    return {"configurable": {"thread_id": thread_id}}


def start_investigation(graph, log_text: str, thread_id: str,
                        title: str = "Uploaded log", source: str = "upload") -> dict:
    """Runs analyze -> diagnose -> suggest_fix, then pauses at approval."""
    register_incident(thread_id, title, source)
    graph.invoke({"log_text": log_text}, _config(thread_id))
    return graph.get_state(_config(thread_id)).values


def resume_investigation(graph, thread_id: str, approved: bool, feedback: str = "") -> dict:
    """Continues after someone approves or rejects."""
    graph.invoke(Command(resume={"approved": approved, "feedback": feedback}), _config(thread_id))
    return graph.get_state(_config(thread_id)).values


def get_investigation(graph, thread_id: str) -> dict:
    return graph.get_state(_config(thread_id)).values


def is_waiting_for_approval(graph, thread_id: str) -> bool:
    return "approval" in graph.get_state(_config(thread_id)).next


def get_status(graph, thread_id: str) -> str:
    """investigating / waiting_for_approval / resolved / stopped"""
    snapshot = graph.get_state(_config(thread_id))
    if "approval" in snapshot.next:
        return "waiting_for_approval"
    if snapshot.values.get("report"):
        return "resolved"
    if not snapshot.next and snapshot.values.get("fix_plan"):
        return "stopped"  # rejected without feedback
    return "investigating"


# ---------- Quick terminal test: run `python graph.py` ----------
if __name__ == "__main__":
    import uuid
    from log_parser import prepare_log

    with open("sample_logs/sample_app.log", encoding="utf-8") as f:
        log_text = prepare_log(f.read())["llm_text"]

    graph = build_graph()
    thread = str(uuid.uuid4())

    print("Running investigation...\n")
    state = start_investigation(graph, log_text, thread, title="Terminal test", source="test")
    print("=== DIAGNOSIS ===\n", state["diagnosis"], "\n")
    print("Status:", get_status(graph, thread))
    print("Saved incidents:", list_incidents(), "\n")

    while is_waiting_for_approval(graph, thread):
        print("=== PROPOSED FIX ===\n", state["fix_plan"], "\n")
        answer = input("Approve this fix? (y/n): ").strip().lower()
        if answer == "y":
            state = resume_investigation(graph, thread, approved=True)
        else:
            feedback = input("Why reject? (leave empty to stop): ").strip()
            state = resume_investigation(graph, thread, approved=False, feedback=feedback)

    print("\nFinal status:", get_status(graph, thread))
    if state.get("report"):
        print("\n=== FINAL REPORT ===\n", state["report"])