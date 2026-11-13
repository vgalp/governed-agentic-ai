"""What gaai can see and do: the platform's services, health, audit chain, profiles, logs and
approvals, read from the running API and the repository; and starting, stopping and switching
profiles through scripts/dev.sh, the same script a person runs.

Every operation is a plain function returning data, so the console, the one-shot commands and the
local assistant (cli/agent.py) all use the same code, and the tests can give it a fake API.
"""

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent.parent
API = os.environ.get("GAAI_API", "http://localhost:8000").rstrip("/")


@dataclass(frozen=True)
class Service:
    name: str
    pattern: str        # how its process looks in `ps` (from dev.sh)
    port: int | None


def services(root: Path = ROOT) -> list[Service]:
    """The services dev.sh starts, read from its SERVICES list: one place to add a service."""
    text = (root / "scripts/dev.sh").read_text()
    block = text[text.index("SERVICES=("):]
    block = block[: block.index("\n)")]
    out = []
    for line in re.findall(r'"([^"]+)"', block):
        name, pattern, _args, port = (line.split("|") + ["", "", "", ""])[:4]
        out.append(Service(name, pattern.replace("\\.", "."), int(port) if port.strip() else None))
    return out


def processes(run=subprocess.run) -> list[str]:
    try:
        return run(["ps", "-Ao", "command="], capture_output=True, text=True, timeout=5).stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        return []


def service_states(procs: list[str] | None = None, root: Path = ROOT) -> list[dict]:
    """Each service: running (one copy), stopped, or duplicated (more than one copy: stale code)."""
    procs = processes() if procs is None else procs
    workers = [p for p in procs if "ops.supervise" not in p and not p.startswith(("grep ", "uv "))]
    out = []
    for s in services(root):
        n = sum(1 for p in workers if s.pattern in p)
        out.append({"name": s.name, "port": s.port, "copies": n,
                    "state": "running" if n == 1 else "stopped" if n == 0 else "duplicated"})
    return out


class Api:
    """The running platform's API. Any failure is an answer ("not answering"), never a crash."""

    def __init__(self, base: str = API, session=None, timeout: float = 5):
        self.base, self.http, self.timeout = base, session or requests.Session(), timeout

    def get(self, path: str, **params) -> dict | list | None:
        try:
            r = self.http.get(self.base + path, params=params or None, timeout=self.timeout)
            return r.json() if r.status_code < 500 or path == "/health" else None
        except (requests.RequestException, ValueError):
            return None

    def post(self, path: str, body: dict) -> dict | None:
        try:
            r = self.http.post(self.base + path, json=body, timeout=self.timeout)
            return r.json() if r.status_code < 400 else {"error": f"{r.status_code}: {r.text[:200]}"}
        except (requests.RequestException, ValueError) as e:
            return {"error": f"the API is not answering ({type(e).__name__})"}

    def health(self):
        return self.get("/health")

    def profile(self):
        return self.get("/profile")

    def audit(self, scope: str = "profile"):
        return self.get("/audit/verify", scope=scope)

    def approvals(self):
        return self.get("/approvals")

    def evaluations(self):
        return self.get("/evaluations")

    def ask(self, message: str, role: str | None, wait: float = 120, poll: float = 0.5, sleep=time.sleep) -> dict:
        """Send a question through the governed pipeline, as the chat page does, and wait for the answer."""
        sent = self.post("/chat", {"message": message, "role": role, "user_id": "gaai-cli"})
        if not sent or "request_id" not in sent:
            return {"error": (sent or {}).get("error", "the API did not accept the message")}
        rid, waited = sent["request_id"], 0.0
        while waited < wait:
            got = self.get(f"/chat/{rid}")
            if got and got.get("status") == "done":
                return got
            sleep(poll)
            waited += poll
        return {"error": f"no answer within {wait:.0f} seconds (request {rid})", "request_id": rid}


def profiles(root: Path = ROOT) -> list[dict]:
    """Every profile: name, title, and where its records come from."""
    out = []
    for d in sorted((root / "profiles").iterdir()):
        f = d / "profile.yaml"
        if not f.exists():
            continue
        try:
            raw = yaml.safe_load(f.read_text()) or {}
        except yaml.YAMLError:
            raw = {}
        records = "none"
        if (d / "fhir.yaml").exists():
            server = (yaml.safe_load((d / "fhir.yaml").read_text()) or {}).get("server") or {}
            host = re.sub(r"^https?://([^/]+).*$", r"\1", server.get("base_url", ""))
            records = f"FHIR {host} ({server.get('auth', 'none')})"
        elif (d / "records.yaml").exists():
            records = "database"
        out.append({"name": d.name, "title": raw.get("title", ""), "records": records})
    return out


def env_profile(root: Path = ROOT) -> str:
    """The profile dev.sh would start: PROFILE from the environment or .env, else its default."""
    if os.environ.get("PROFILE"):
        return os.environ["PROFILE"]
    env = root / ".env"
    if env.exists():
        m = re.search(r"^PROFILE=(\S+)", env.read_text(), re.M)
        if m:
            return m.group(1).strip("'\"")
    return "adhd-assistant"


def log_tail(service: str, lines: int = 40, root: Path = ROOT) -> list[str]:
    """The last lines of a service's log. Logs hold no patient text: the pipeline logs masked text."""
    if not re.fullmatch(r"[a-z][a-z-]*", service):
        raise ValueError(f"not a service name: {service}")
    f = root / "logs" / f"{service}.log"
    if not f.exists():
        return []
    return f.read_text(errors="replace").splitlines()[-max(1, min(lines, 400)):]


def run_script(args: list[str], env: dict | None = None, root: Path = ROOT, run=subprocess.run,
               timeout: float = 600, on_line=None) -> dict:
    """Run one of the repository's scripts (dev.sh, fhir_check.py, an eval) and return its output.
    With `on_line`, each line is passed on as it is printed (a start takes a minute: show progress)."""
    env = {**os.environ, **(env or {})}
    try:
        if on_line is None:
            p = run(args, cwd=root, env=env, capture_output=True, text=True, timeout=timeout)
            return {"ok": p.returncode == 0, "code": p.returncode, "output": (p.stdout + p.stderr)[-6000:]}
        lines = []
        with subprocess.Popen(args, cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, bufsize=1) as proc:
            for line in proc.stdout:
                lines.append(line.rstrip("\n"))
                on_line(lines[-1])
            code = proc.wait(timeout=timeout)
        return {"ok": code == 0, "code": code, "output": "\n".join(lines)[-6000:]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "code": None, "output": f"stopped after {timeout:.0f} seconds"}
    except OSError as e:
        return {"ok": False, "code": None, "output": str(e)}


def dev(action: str, profile: str | None = None, **kw) -> dict:
    if action not in ("start", "stop", "restart", "status"):
        raise ValueError(action)
    return run_script(["./scripts/dev.sh", action], env={"PROFILE": profile} if profile else None, **kw)


def fhir_check(profile: str, patient: str | None = None, **kw) -> dict:
    args = [".venv/bin/python", "scripts/fhir_check.py", "--profile", profile]
    if patient:
        args += ["--patient", patient]
    return run_script(args, timeout=180, **kw)


def snapshot(api: Api | None = None, procs: list[str] | None = None, root: Path = ROOT) -> dict:
    """Everything the start screen shows, in one call."""
    api = api or Api()
    svc = service_states(procs, root)
    prof = api.profile()
    audit = api.audit() if prof else None
    health = api.health() if prof else None
    pending = [a for a in (api.approvals() or []) if isinstance(a, dict) and a.get("status") == "pending"] if prof else []
    return {"services": svc, "running": sum(s["state"] == "running" for s in svc), "total": len(svc),
            "profile": (prof or {}).get("name") or None, "title": (prof or {}).get("title"),
            "next_profile": env_profile(root), "health": health, "audit": audit, "pending": len(pending)}


def to_json(data) -> str:
    return json.dumps(data, indent=1, default=str)
