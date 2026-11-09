"""Run a service and restart it when it stops (audit OC-3, ADR 030).

    python -m ops.supervise <name> -- <module and arguments>
    python -m ops.supervise planner -- agents.planner

The service runs as `python -m <module and arguments>`. When it exits, it is started again after a
short wait that grows with each quick crash (1, 2, 4 ... up to 60 seconds). If it crashes 5 times
within 2 minutes, the supervisor gives up and says so in the service's health file, so /health
reports it instead of a restart loop hiding it. SIGTERM or SIGINT stops the service and the
supervisor (scripts/dev.sh stop).
"""

import signal
import subprocess
import sys
import time

from ops.health import Heartbeat

MAX_CRASHES = 5
WINDOW = 120
MAX_WAIT = 60


def backoff(recent_crashes: int) -> float:
    return min(MAX_WAIT, 2 ** max(0, recent_crashes - 1))


def run(name: str, argv: list[str], popen=subprocess.Popen, sleep=time.sleep, clock=time.time) -> int:
    hb = Heartbeat(f"{name}.supervisor")
    stopping = {"now": False}
    child = {"p": None}

    def stop(signum, frame):
        stopping["now"] = True
        if child["p"] is not None and child["p"].poll() is None:
            child["p"].terminate()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    crashes: list[float] = []
    restarts = 0
    while not stopping["now"]:
        started = clock()
        hb.set("running", restarts=restarts)
        child["p"] = popen([sys.executable, "-m", *argv])
        code = child["p"].wait()
        if stopping["now"]:
            break
        crashes = [t for t in crashes if clock() - t < WINDOW] + [clock()]
        print(f"[supervise] {name} exited with code {code} after {clock() - started:.0f} s "
              f"({len(crashes)} exit(s) in the last {WINDOW} s)", flush=True)
        if len(crashes) >= MAX_CRASHES:
            hb.set("gave_up", restarts=restarts, last_exit=code)
            print(f"[supervise] {name} stopped {MAX_CRASHES} times in {WINDOW} s: not restarting it. "
                  "See its log, fix the cause, then ./scripts/dev.sh restart", flush=True)
            return 1
        wait = backoff(len(crashes))
        hb.set("restarting", restarts=restarts, last_exit=code)
        sleep(wait)
        restarts += 1
    if child["p"] is not None:
        try:
            child["p"].wait(timeout=10)
        except subprocess.TimeoutExpired:
            child["p"].kill()
    hb.set("stopped", restarts=restarts)
    return 0


def main(args: list[str]) -> int:
    if len(args) < 3 or args[1] != "--":
        print(__doc__)
        return 2
    return run(args[0], args[2:])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
