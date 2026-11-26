"""
Fake application that keeps writing logs, then injects a fault.

Examples:
    python fake_app.py                                  # db_pool fault after 60s
    python fake_app.py --fault payment_api --fault-after 30
    python fake_app.py --fault memory_leak
    python fake_app.py --fault none                     # normal traffic only
Stop with Ctrl+C.
"""
import argparse
import random
import time
from datetime import datetime
from pathlib import Path

LOG_FILE = Path("logs/live_app.log")


def write(f, level: str, service: str, message: str):
    """Write one line in the same format log_parser.py expects."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts} {level:<5} [{service}] {message}"
    f.write(line + "\n")
    f.flush()  # so the watcher sees it right away
    print(line)


# ---------- Normal traffic ----------
def normal_line():
    if random.random() < 0.03:  # occasional harmless warning (noise)
        return ("WARN", "api-gateway", f"Slow request: GET /orders took {random.randint(900, 1500)}ms")

    order = random.randint(1000, 9999)
    return random.choice([
        ("INFO", "api-gateway", f"GET /orders 200 {random.randint(30, 90)}ms"),
        ("INFO", "api-gateway", f"POST /orders 201 {random.randint(80, 160)}ms"),
        ("INFO", "order-service", f"Order #{order} created for user {random.randint(1000, 9999)}"),
        ("INFO", "payment-service", f"Payment for order #{order} succeeded"),
        ("INFO", "recommendation-service", f"GET /recommendations 200 {random.randint(40, 120)}ms"),
        ("INFO", "api-gateway", "GET /health 200 3ms"),
    ])


# ---------- Fault scenarios ----------
def db_pool_lines(step):
    order = random.randint(1000, 9999)
    return random.choice([
        ("WARN", "order-service", "Connection pool usage at 100% (5/5 in use)"),
        ("ERROR", "order-service", "Timeout acquiring DB connection after 5000ms"),
        ("ERROR", "order-service", f"Failed to create order for user {random.randint(1000, 9999)}: DBConnectionTimeout"),
        ("ERROR", "api-gateway", f"POST /orders 500 {random.randint(5000, 5020)}ms"),
        ("ERROR", "payment-service", f"Payment aborted for order #{order}: upstream order-service returned 500"),
    ])


def payment_api_lines(step):
    order = random.randint(1000, 9999)
    return random.choice([
        ("WARN", "payment-service", f"PayGate response slow: {random.randint(3000, 4900)}ms"),
        ("ERROR", "payment-service", "PayGate request timed out after 5000ms"),
        ("ERROR", "payment-service", f"Payment failed for order #{order}: PayGateTimeout"),
        ("ERROR", "api-gateway", f"POST /checkout 504 {random.randint(5000, 5030)}ms"),
    ])


def memory_leak_lines(step):
    heap = min(70 + step * 2, 99)  # memory keeps growing
    if heap < 92:
        return ("WARN", "recommendation-service", f"Heap usage at {heap}% (GC running frequently)")
    return random.choice([
        ("ERROR", "recommendation-service", "java.lang.OutOfMemoryError: Java heap space"),
        ("ERROR", "recommendation-service", "Request handler crashed: OutOfMemoryError"),
        ("ERROR", "api-gateway", "GET /recommendations 503 2ms (upstream unavailable)"),
    ])


SCENARIOS = {
    "db_pool": {
        "start": [
            ("INFO", "deploy", "Deployed order-service v2.4.1 (config change: db.pool_size 20 -> 5)"),
            ("INFO", "order-service", "Connected to database orders_db (pool_size=5)"),
        ],
        "lines": db_pool_lines,
    },
    "payment_api": {
        "start": [
            ("INFO", "payment-service", "Health check: PayGate external API latency rising"),
        ],
        "lines": payment_api_lines,
    },
    "memory_leak": {
        "start": [
            ("INFO", "deploy", "Deployed recommendation-service v1.8.0 (new in-memory result cache)"),
        ],
        "lines": memory_leak_lines,
    },
}


def main():
    parser = argparse.ArgumentParser(description="Fake app that writes live logs")
    parser.add_argument("--fault", choices=[*SCENARIOS, "none"], default="db_pool")
    parser.add_argument("--fault-after", type=float, default=60, help="seconds before the fault starts")
    parser.add_argument("--speed", type=float, default=1.0, help="seconds between log lines")
    args = parser.parse_args()

    LOG_FILE.parent.mkdir(exist_ok=True)
    scenario = SCENARIOS.get(args.fault)
    start_time = time.time()
    fault_started = False
    step = 0

    print(f"Writing to {LOG_FILE} | fault: {args.fault} | starts after {args.fault_after}s\n")

    with LOG_FILE.open("a", encoding="utf-8") as f:
        write(f, "INFO", "api-gateway", "Server started on port 8080")
        try:
            while True:
                # Start the fault once enough time has passed
                if scenario and not fault_started and time.time() - start_time >= args.fault_after:
                    for line in scenario["start"]:
                        write(f, *line)
                    fault_started = True

                # During a fault, about 60% of lines are fault lines
                if fault_started and random.random() < 0.6:
                    write(f, *scenario["lines"](step))
                    step += 1
                else:
                    write(f, *normal_line())

                time.sleep(args.speed)
        except KeyboardInterrupt:
            print("\nStopped.")


if __name__ == "__main__":
    main()