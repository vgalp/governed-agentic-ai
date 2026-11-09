"""Run several tool servers in one container, each under its own supervisor (ADR 031).

    python -m ops.run_all records changes drug-reference
    TOOL_SERVERS="records,changes" python -m ops.run_all

A cloud deployment runs the tool servers next to the planner and the executor (the same task or
container app, so they share 127.0.0.1 and the profiles' tool addresses stay as they are). One
container for all of them keeps within the platforms' limit on containers per task. Each server is
restarted by ops.supervise; if a supervisor gives up, this process exits and the platform restarts
the container. SIGTERM stops them all.
"""

import os
import signal
import subprocess
import sys
import time

ALL = ["knowledge-base", "records", "changes", "medication-review", "fhir-records", "web-research",
       "chart-brief", "drug-reference"]


def module(name: str) -> str:
    if name not in ALL:
        raise SystemExit(f"unknown tool server {name!r}; known: {', '.join(ALL)}")
    return f"mcp_servers.{name.replace('-', '_')}.server"


def wanted(argv: list[str], env=os.environ) -> list[str]:
    names = argv or [n.strip() for n in env.get("TOOL_SERVERS", "").split(",") if n.strip()] or ALL
    return [n for n in dict.fromkeys(names)]


def main(argv: list[str], popen=subprocess.Popen, sleep=time.sleep) -> int:
    names = wanted(argv)
    children = {n: popen([sys.executable, "-m", "ops.supervise", n, "--", module(n)]) for n in names}
    print(f"tool servers: {', '.join(names)}", flush=True)

    def stop(signum, frame):
        for p in children.values():
            if p.poll() is None:
                p.terminate()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while True:
        for n, p in children.items():
            code = p.poll()
            if code is not None:
                print(f"{n}: its supervisor stopped ({code}); stopping the others", flush=True)
                stop(None, None)
                for q in children.values():
                    q.wait()
                return code or 1
        sleep(1)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
