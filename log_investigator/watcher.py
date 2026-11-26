"""
Watches logs/live_app.log and starts an investigation when errors spike.

    python watcher.py
    python watcher.py --threshold 5 --window 30     # more sensitive
Stop with Ctrl+C.
"""
import argparse
import time
import uuid
from collections import deque
from datetime import datetime
from pathlib import Path

from log_parser import prepare_log, parse_log
from graph import build_graph, start_investigation, get_status

LOG_FILE = Path("logs/live_app.log")
ERROR_LEVELS = {"ERROR", "CRITICAL", "FATAL"}


def read_new_lines(position: int):
    """Read only the lines added since last time (like `tail -f`)."""
    if not LOG_FILE.exists():
        return [], 0
    if LOG_FILE.stat().st_size < position:
        position = 0  # file was deleted or recreated: start over

    with LOG_FILE.open("rb") as f:
        f.seek(position)
        data = f.read()

    end = data.rfind(b"\n") + 1  # only take complete lines
    lines = data[:end].decode("utf-8", errors="replace").splitlines()
    return [line for line in lines if line.strip()], position + end


def is_error(line: str) -> bool:
    entries = parse_log(line)
    return bool(entries) and entries[0]["level"] in ERROR_LEVELS


def trigger_investigation(graph, buffer, error_count: int, window: int):
    """Send the recent logs through the same pipeline as the upload version."""
    log_text = "\n".join(line for _, line, _ in buffer)
    log = prepare_log(log_text)

    services = log["summary"]["errors_by_service"]
    top_service = max(services, key=services.get) if services else "unknown"
    title = f"{top_service}: {error_count} errors in {window}s"
    thread_id = str(uuid.uuid4())

    print(f"\n>>> ERROR SPIKE DETECTED: {title}")
    print(f">>> Starting investigation {thread_id[:8]} (this takes a little while)...")
    try:
        start_investigation(graph, log["llm_text"], thread_id, title=title, source="watcher")
        print(f">>> Done. Status: {get_status(graph, thread_id)}. Review it in the app.\n")
    except Exception as e:
        print(f">>> Investigation failed: {e}\n")


def main():
    parser = argparse.ArgumentParser(description="Watch the live log for error spikes")
    parser.add_argument("--threshold", type=int, default=10, help="errors needed to trigger")
    parser.add_argument("--window", type=int, default=60, help="seconds to count errors over")
    parser.add_argument("--context", type=int, default=180, help="seconds of logs sent to Claude")
    parser.add_argument("--cooldown", type=int, default=300, help="seconds before it can trigger again")
    parser.add_argument("--interval", type=float, default=5, help="seconds between checks")
    parser.add_argument("--from-start", action="store_true", help="also read lines already in the file")
    args = parser.parse_args()

    graph = build_graph()

    # Start at the end of the file, so only NEW lines are watched
    position = 0
    if LOG_FILE.exists() and not args.from_start:
        position = LOG_FILE.stat().st_size

    buffer = deque()  # (time received, line, is_error)
    last_trigger = 0.0

    print(f"Watching {LOG_FILE} | trigger: {args.threshold}+ errors in {args.window}s "
          f"| cooldown: {args.cooldown}s\n")

    try:
        while True:
            new_lines, position = read_new_lines(position)
            now = time.time()

            for line in new_lines:
                buffer.append((now, line, is_error(line)))

            # Forget lines older than the context window
            while buffer and buffer[0][0] < now - args.context:
                buffer.popleft()

            recent_errors = sum(1 for t, _, err in buffer if err and t >= now - args.window)
            clock = datetime.now().strftime("%H:%M:%S")
            print(f"[{clock}] +{len(new_lines)} lines | {recent_errors} errors in last {args.window}s")

            in_cooldown = now - last_trigger < args.cooldown
            if recent_errors >= args.threshold and not in_cooldown:
                last_trigger = now
                trigger_investigation(graph, buffer, recent_errors, args.window)

            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nWatcher stopped.")


if __name__ == "__main__":
    main()