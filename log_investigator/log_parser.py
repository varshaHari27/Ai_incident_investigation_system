import re
from collections import Counter

# Matches lines like: 2026-11-20 10:40:31 ERROR [order-service] Timeout acquiring DB connection
LOG_PATTERN = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})\S*\s+"  # date and time
    r"(?P<level>[A-Z]+)\s+"                                          # INFO / WARN / ERROR
    r"(?:\[(?P<service>[^\]]+)\]\s+)?"                               # [service] (optional)
    r"(?P<message>.*)$"                                              # the rest
)

IMPORTANT_LEVELS = {"ERROR", "WARN", "WARNING", "CRITICAL", "FATAL"}
ERROR_WORDS = re.compile(r"error|exception|fail|timeout|refused|denied|crash", re.IGNORECASE)


def parse_log(text: str) -> list[dict]:
    """Turn raw log text into a list of structured entries."""
    entries = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue

        match = LOG_PATTERN.match(line)
        if match:
            entry = match.groupdict()
            entry["service"] = entry["service"] or "unknown"
        elif entries:
            # No timestamp: probably a continuation (like a stack trace) of the previous line
            prev = entries[-1]
            entry = {"timestamp": prev["timestamp"], "level": prev["level"],
                     "service": prev["service"], "message": line}
        else:
            entry = {"timestamp": None, "level": "UNKNOWN", "service": "unknown", "message": line}

        entry["line_no"] = line_no
        entry["raw"] = line
        entries.append(entry)
    return entries


def is_important(entry: dict) -> bool:
    """Error/warning level, or an error-sounding message."""
    return entry["level"] in IMPORTANT_LEVELS or bool(ERROR_WORDS.search(entry["message"]))


def get_important_lines(entries: list[dict], context: int = 3) -> list[dict]:
    """Important lines plus the few lines before each one (the cause is often just before)."""
    keep = set()
    for i, entry in enumerate(entries):
        if is_important(entry):
            keep.update(range(max(0, i - context), i + 1))
    return [entries[i] for i in sorted(keep)]


def summarize(entries: list[dict]) -> dict:
    """Basic stats about the log."""
    errors = [e for e in entries if e["level"] in {"ERROR", "CRITICAL", "FATAL"}]
    timestamps = [e["timestamp"] for e in entries if e["timestamp"]]
    return {
        "total_lines": len(entries),
        "time_range": f"{timestamps[0]} to {timestamps[-1]}" if timestamps else "unknown",
        "level_counts": dict(Counter(e["level"] for e in entries)),
        "errors_by_service": dict(Counter(e["service"] for e in errors)),
        "first_error": errors[0]["raw"] if errors else "none",
    }


def format_for_llm(summary: dict, important: list[dict], max_lines: int = 300) -> str:
    """Build the text that gets sent to Claude."""
    parts = [
        "LOG SUMMARY",
        f"Total lines: {summary['total_lines']}",
        f"Time range: {summary['time_range']}",
        f"Level counts: {summary['level_counts']}",
        f"Errors by service: {summary['errors_by_service']}",
        f"First error: {summary['first_error']}",
        "",
        "IMPORTANT LINES (errors/warnings with the lines just before them)",
    ]

    prev_line_no = None
    for entry in important[:max_lines]:
        if prev_line_no is not None and entry["line_no"] > prev_line_no + 1:
            parts.append("   ...")  # marks a gap between sections
        parts.append(f"L{entry['line_no']}: {entry['raw']}")
        prev_line_no = entry["line_no"]

    if len(important) > max_lines:
        parts.append(f"... ({len(important) - max_lines} more lines not shown)")
    return "\n".join(parts)


def prepare_log(text: str) -> dict:
    """Main function: raw log text in, everything the app needs out."""
    entries = parse_log(text)
    summary = summarize(entries)
    important = get_important_lines(entries)
    return {
        "entries": entries,
        "summary": summary,
        "llm_text": format_for_llm(summary, important),
    }


if __name__ == "__main__":
    # Quick test: run `python log_parser.py`
    with open("sample_logs/sample_app.log", encoding="utf-8") as f:
        result = prepare_log(f.read())
    print(result["llm_text"])