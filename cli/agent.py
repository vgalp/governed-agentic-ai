"""The console's assistant: a local model (Qwen, through Ollama) that operates the platform with the
same functions the console's commands use (cli/platform.py). ADR 033.

The platform's own rules apply to its operator assistant too:
  - it runs on this machine: nothing it reads leaves the computer;
  - it sees operations data only: service states, health, the audit chain's result, logs (masked
    text), profiles, approvals' status. It has no tool that reads a patient record or sends a
    question through the pipeline;
  - it reads freely, but every change (start, stop, restart, switch profile) is proposed and waits
    for the person's yes, as changes in the pipeline wait for an approver;
  - it may not approve anything in the pipeline: approvals stay with a signed-in approver.
"""

import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass

import requests

from cli import platform as pf

OLLAMA = (os.environ.get("OLLAMA_HOST") or "http://localhost:11434").rstrip("/")
if "://" not in OLLAMA:
    OLLAMA = f"http://{OLLAMA}"
MODEL = os.environ.get("GAAI_MODEL", "qwen3:8b")
MAX_STEPS = 8

SYSTEM = """You are gaai, the operator assistant for a governed agentic AI platform running on this computer.
The platform puts safety, privacy and audit checks around AI assistants for clinics, public offices and schools.
You help the operator run it: check what is running, read health and logs, verify the audit chain, list profiles,
check a record-system connection, and start, stop or switch profiles.

Rules:
- Use the tools to find facts; never guess the platform's state. Call a tool before answering about it.
- Changes (start, stop, restart, switching profile) are made by calling the tool: the console then asks the operator
  "Go ahead? [y/N]" and runs it only on yes. When the operator asks for a change, call the tool; do not ask them to
  confirm in words first. If they said no before and now ask again, call the tool again.
- Those yes/no questions are not the pipeline's approvals. Approvals on the web page are for changes the assistant
  pipeline proposes (e.g. an appointment update); they have nothing to do with starting or switching the platform.
- You cannot read patient records or send questions through the assistant pipeline, and you cannot approve
  pending changes: point the operator to the web page (http://localhost:8000) for those.
- Answer in short plain sentences. Name services, profiles and commands exactly. If something failed, quote the
  relevant log line and say what to try next.
/no_think"""


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    params: dict                      # JSON schema properties
    run: Callable[..., object]
    changes: bool = False             # proposes a change: needs the operator's yes
    required: tuple[str, ...] = ()

    def schema(self) -> dict:
        return {"type": "function", "function": {
            "name": self.name, "description": self.description,
            "parameters": {"type": "object", "properties": self.params, "required": list(self.required)}}}


def tools(api: pf.Api | None = None, on_line=None) -> dict[str, Tool]:
    """The assistant's tools. `on_line` receives a start or restart's output as it is printed."""
    api = api or pf.Api()
    profile_names = [p["name"] for p in pf.profiles()]
    service_names = [s.name for s in pf.services()]
    prof = {"type": "string", "enum": profile_names, "description": "a profile name"}
    return {t.name: t for t in [
        Tool("status", "Which services are running, the active profile, health, audit chain and pending approvals.",
             {}, lambda: pf.snapshot(api)),
        Tool("health", "The platform's health report: Kafka, readers, planner and executor, delivery.",
             {}, lambda: api.health() or {"status": "not answering", "note": "the API on port 8000 is not running"}),
        Tool("audit_verify", "Verify the tamper-evident audit chain (scope: profile or all).",
             {"scope": {"type": "string", "enum": ["profile", "all"]}},
             lambda scope="profile": api.audit(scope) or {"note": "the API is not answering"}),
        Tool("profiles", "List the profiles that can be started, with where their records come from.",
             {}, lambda: pf.profiles()),
        Tool("logs", "The last lines of one service's log.",
             {"service": {"type": "string", "enum": service_names}, "lines": {"type": "integer"}},
             lambda service, lines=40: pf.log_tail(service, int(lines)), required=("service",)),
        Tool("approvals", "Pending and recent change approvals (status and masked text only).",
             {}, lambda: [{k: a.get(k) for k in ("id", "status", "action", "created_at")} for a in (api.approvals() or [])
                          if isinstance(a, dict)][:20]),
        Tool("fhir_check", "Check a profile's connection to its FHIR record system (sign-in, capabilities). "
             "No patient is read.", {"profile": prof}, lambda profile: pf.fhir_check(profile), required=("profile",)),
        Tool("start", "Start the platform with a profile.", {"profile": prof},
             lambda profile=None: pf.dev("start", profile, on_line=on_line), changes=True),
        Tool("restart", "Restart the platform, optionally switching to another profile.", {"profile": prof},
             lambda profile=None: pf.dev("restart", profile, on_line=on_line), changes=True),
        Tool("stop", "Stop the platform's services.", {}, lambda: pf.dev("stop", on_line=on_line), changes=True),
    ]}


def _clean(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()


class Agent:
    """One conversation with the local model. `confirm(tool, args)` asks the operator before a change;
    `show(event, data)` lets the console display each step."""

    def __init__(self, tools_: dict[str, Tool], confirm: Callable[[Tool, dict], bool],
                 show: Callable[[str, dict], None] = lambda e, d: None, model: str = MODEL,
                 host: str = OLLAMA, session=None):
        self.tools, self.confirm, self.show = tools_, confirm, show
        self.model, self.host, self.http = model, host, session or requests.Session()
        self.messages: list[dict] = [{"role": "system", "content": SYSTEM}]

    def available(self) -> tuple[bool, str]:
        """Is Ollama running, and does it have the model?"""
        try:
            names = [m.get("name", "") for m in self.http.get(f"{self.host}/api/tags", timeout=3).json().get("models", [])]
        except (requests.RequestException, ValueError):
            return False, f"Ollama is not running at {self.host}. Start it with: ollama serve"
        base = self.model.split(":")[0]
        if self.model in names or (":" not in self.model and any(n.split(":")[0] == base for n in names)):
            return True, self.model
        return False, f"The model {self.model} is not in Ollama. Get it with: ollama pull {self.model}"

    def _chat(self) -> dict:
        r = self.http.post(f"{self.host}/api/chat", timeout=300, json={
            "model": self.model, "messages": self.messages, "stream": False,
            "tools": [t.schema() for t in self.tools.values()], "options": {"temperature": 0.2}})
        if r.status_code != 200:
            raise RuntimeError(f"Ollama answered {r.status_code}: {r.text[:200]}")
        return r.json().get("message") or {}

    def call(self, name: str, args: dict) -> object:
        tool = self.tools.get(name)
        if tool is None:
            return {"error": f"no tool named {name}"}
        args = {k: v for k, v in (args or {}).items() if k in tool.params}
        if tool.changes and not self.confirm(tool, args):
            return {"declined": True, "note": "The operator answered no to this one proposal, so it was not done. "
                    "Say so in one sentence. If they ask for it again, propose it again: they will be asked again."}
        self.show("tool", {"name": name, "args": args, "changes": tool.changes})
        try:
            return tool.run(**args)
        except Exception as e:  # noqa: BLE001  a failed tool is an answer for the model, not a crash
            return {"error": f"{type(e).__name__}: {e}"}

    def ask(self, text: str) -> str:
        self.messages.append({"role": "user", "content": text})
        for _ in range(MAX_STEPS):
            try:
                msg = self._chat()
            except (requests.RequestException, RuntimeError) as e:
                self.messages.pop()
                return f"The local model did not answer ({e})."
            calls = msg.get("tool_calls") or []
            self.messages.append({"role": "assistant", "content": msg.get("content", ""), "tool_calls": calls})
            if not calls:
                return _clean(msg.get("content", "")) or "(no answer)"
            for c in calls:
                fn = c.get("function") or {}
                args = fn.get("arguments") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except ValueError:
                        args = {}
                result = self.call(fn.get("name", ""), args)
                self.messages.append({"role": "tool", "tool_name": fn.get("name", ""),
                                      "content": pf.to_json(result)[:8000]})
        return "I stopped after several steps without an answer. Try asking more specifically."

    def reset(self) -> None:
        self.messages = self.messages[:1]
