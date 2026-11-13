"""The gaai console: a start screen with the platform's state, slash commands, and the local
assistant for everything else (ADR 033).

    ./gaai                   open the console
    /help                    the commands
    anything else            asked of the local assistant (Qwen through Ollama)
"""

import os
import shlex
from pathlib import Path

from cli import platform as pf
from cli import ui
from cli.agent import Agent, Tool, tools

HISTORY = Path("~/.config/governed-agentic-ai/gaai_history").expanduser()

COMMANDS = {
    "/status": "the start screen again: services, profile, health, audit, approvals",
    "/services": "each service and its port",
    "/health": "the health report (Kafka, readers, planner, executor)",
    "/audit": "verify the audit chain (/audit all: every profile)",
    "/profiles": "the profiles, and where their records come from",
    "/use": "switch profile: restart with it (/use clinic-oracle)",
    "/start": "start the platform (/start [profile])",
    "/restart": "restart the platform (/restart [profile])",
    "/stop": "stop the platform's services",
    "/logs": "the end of a service's log (/logs chart-brief 60)",
    "/approvals": "changes waiting for an approver",
    "/ask": "send a question through the governed pipeline (/ask clinician What is our cancellation policy?)",
    "/check": "check a profile's record system (/check clinic-oracle [Wilm Smart 1990-09-15])",
    "/new": "build a new office profile from a few questions (Qwen drafts; tested before it is saved)",
    "/model": "show or change the assistant's model (/model qwen3:8b)",
    "/clear": "forget the conversation with the assistant",
    "/quit": "leave (or Ctrl-D)",
}


def status_lines(snap: dict, assistant: tuple[bool, str]) -> list[str]:
    run, total = snap["running"], snap["total"]
    tone = "ok" if run == total else "bad" if run == 0 else "warn"
    lines = []
    if snap["profile"]:
        title = ui.style(snap.get("title") or "", "grey")
        lines.append(f"{ui.mark('ok')} profile   {ui.style(snap['profile'], 'bold')}  {title}")
    else:
        lines.append(f"{ui.mark('info')} profile   not running; {ui.style('/start', 'cyan')} starts "
                     f"{ui.style(snap['next_profile'], 'bold')}")
    down = [s["name"] for s in snap["services"] if s["state"] != "running"]
    lines.append(f"{ui.mark(tone)} services  {run} of {total} running"
                 + (ui.style(f"  (not running: {', '.join(down)})", "grey") if down and run else ""))
    h = snap.get("health")
    if h:
        state = h.get("status", "?")
        lines.append(f"{ui.mark({'ok': 'ok', 'degraded': 'warn'}.get(state, 'bad'))} health    {state}"
                     + (ui.style("  " + "; ".join((h.get("problems") or []) + (h.get("warnings") or []))[:90], "grey")
                        if state != "ok" else ""))
    a = snap.get("audit")
    if a:
        lines.append(f"{ui.mark('ok' if a.get('intact') else 'bad')} audit     "
                     + (f"chain intact: {a.get('events', 0)} events in {a.get('chains', 0)} chain(s)" if a.get("intact")
                        else f"{len(a.get('problems') or [])} problem(s): run /audit"))
    if snap["profile"]:
        n = snap.get("pending", 0)
        hint = ui.style("  (/approvals)", "grey") if n else ""
        lines.append(f"{ui.mark('warn' if n else 'ok')} approvals {n} waiting{hint}")
    ok, what = assistant
    lines.append(f"{ui.mark('ok' if ok else 'warn')} assistant "
                 + (f"{what}, on this computer" if ok else ui.style(what, "amber")))
    return lines


class Shell:
    def __init__(self, api: pf.Api | None = None, agent: Agent | None = None, read=input, write=print,
                 procs=None):
        self.api, self.read, self.write, self.procs = api or pf.Api(), read, write, procs
        self.agent = agent or Agent(tools(self.api, on_line=lambda t: self.show_step("line", {"text": t})),
                                    confirm=self.confirm, show=self.show_step)

    # --- talking to the operator ------------------------------------------------------------------

    def confirm(self, tool: Tool, args: dict) -> bool:
        what = {"start": "Start the platform", "restart": "Restart the platform", "stop": "Stop the platform"}.get(
            tool.name, tool.name)
        if args.get("profile"):
            what += f" with profile {ui.style(args['profile'], 'bold')}"
        try:
            with ui.paused():
                answer = self.read(f"  {ui.style('?', 'amber')} {what}. Go ahead? [y/N] ")
        except (EOFError, KeyboardInterrupt):
            answer = ""
        yes = answer.strip().lower() in ("y", "yes")
        if not yes:
            self.write(ui.style("  not done (you answered no)", "grey"))
        return yes

    def show_step(self, event: str, data: dict) -> None:
        if event == "tool":
            args = ", ".join(f"{k}={v}" for k, v in data["args"].items())
            with ui.paused():
                self.write(ui.style(f"  ⎿ {data['name']}({args})", "grey"))
        elif event == "line":                     # a start or restart's output, shown as it comes
            with ui.paused():
                self.write(ui.style("  │ ", "grey") + data["text"])

    def screen(self) -> None:
        with ui.spinner("reading the platform's state"):
            snap = pf.snapshot(self.api, self.procs)
            assistant = self.agent.available()
        self.write(ui.panel("status", status_lines(snap, assistant)))

    # --- commands ----------------------------------------------------------------------------------

    def do(self, line: str) -> bool:
        """Run one line. False: leave."""
        line = line.strip()
        if not line:
            return True
        if not line.startswith("/"):
            ok, what = self.agent.available()
            if not ok:
                self.write(f"  {ui.mark('warn')} {what}\n  {ui.style('The commands work without it: /help', 'grey')}")
                return True
            with ui.spinner(f"thinking ({self.agent.model})"):
                answer = self.agent.ask(line)
            self.write("\n" + answer + "\n")
            return True
        try:
            parts = shlex.split(line)
        except ValueError:
            parts = line.split()
        cmd, args = parts[0].lower(), parts[1:]
        if cmd in ("/quit", "/exit", "/q"):
            return False
        handler = getattr(self, "cmd_" + cmd[1:], None)
        if handler is None:
            self.write(f"  {ui.mark('bad')} no command {cmd}. {ui.style('/help', 'cyan')} lists them.")
            return True
        try:
            handler(args)
        except (ValueError, KeyError) as e:
            self.write(f"  {ui.mark('bad')} {e}")
        return True

    def cmd_help(self, args):
        rows = [[ui.style(c, "cyan"), d] for c, d in COMMANDS.items()]
        self.write(ui.table(rows) + "\n\n" + ui.style("Anything else is asked of the assistant, e.g. "
                                                       "\"why is the planner not running?\"", "grey"))

    def cmd_status(self, args):
        self.screen()

    def cmd_services(self, args):
        rows = [[ui.mark({"running": "ok", "stopped": "bad"}.get(s["state"], "warn")), s["name"],
                 str(s["port"] or "–"), s["state"] + (f" ({s['copies']} copies)" if s["copies"] > 1 else "")]
                for s in pf.service_states(self.procs)]
        self.write(ui.table(rows, ["", "service", "port", "state"]))

    def cmd_health(self, args):
        h = self.api.health()
        if not h:
            self.write(f"  {ui.mark('bad')} the API is not answering on port 8000")
            return
        self.write(f"  {ui.mark({'ok': 'ok', 'degraded': 'warn'}.get(h.get('status'), 'bad'))} {h.get('status')}")
        for p in h.get("problems") or []:
            self.write(f"    {ui.mark('bad')} {p}")
        for w in h.get("warnings") or []:
            self.write(f"    {ui.mark('warn')} {w}")

    def cmd_audit(self, args):
        a = self.api.audit("all" if args[:1] == ["all"] else "profile")
        if not a:
            self.write(f"  {ui.mark('bad')} the API is not answering on port 8000")
            return
        self.write(f"  {ui.mark('ok' if a.get('intact') else 'bad')} {a.get('scope')}: {a.get('events')} events, "
                   f"{a.get('chains')} chain(s), {'intact' if a.get('intact') else 'NOT intact'}")
        for p in a.get("problems") or []:
            self.write(f"    {ui.mark('bad')} {p}")

    def cmd_profiles(self, args):
        current = (self.api.profile() or {}).get("name")
        rows = [[ui.mark("ok") if p["name"] == current else " ", ui.style(p["name"], "bold"), p["records"],
                 ui.style(p["title"], "grey")] for p in pf.profiles()]
        self.write(ui.table(rows, ["", "profile", "records", "title"]))

    def _dev(self, action: str, profile: str | None):
        if profile and profile not in {p["name"] for p in pf.profiles()}:
            raise ValueError(f"no profile named {profile} (/profiles lists them)")
        tool = self.agent.tools[action]
        if not self.confirm(tool, {"profile": profile} if profile else {}):
            return
        res = pf.dev(action, profile, on_line=lambda s: self.write(ui.style("  │ ", "grey") + s))
        self.write(f"  {ui.mark('ok' if res['ok'] else 'bad')} {action} {'done' if res['ok'] else 'failed'}")

    def cmd_start(self, args):
        self._dev("start", args[0] if args else None)

    def cmd_restart(self, args):
        self._dev("restart", args[0] if args else None)

    def cmd_use(self, args):
        if not args:
            raise ValueError("which profile? e.g. /use clinic-oracle")
        self._dev("restart", args[0])

    def cmd_stop(self, args):
        self._dev("stop", None)

    def cmd_logs(self, args):
        if not args:
            raise ValueError("which service? e.g. /logs chart-brief (/services lists them)")
        lines = pf.log_tail(args[0], int(args[1]) if len(args) > 1 else 40)
        self.write("\n".join(lines) if lines else ui.style(f"  no log for {args[0]} yet", "grey"))

    def cmd_approvals(self, args):
        items = [a for a in (self.api.approvals() or []) if isinstance(a, dict)]
        if not items:
            self.write(ui.style("  no approvals", "grey"))
            return
        rows = [[ui.mark("warn" if a.get("status") == "pending" else "info"), str(a.get("id", ""))[:8],
                 str(a.get("status", "")), str(a.get("action", "")), str(a.get("created_at", ""))[:19]] for a in items[:20]]
        self.write(ui.table(rows, ["", "id", "status", "action", "created"]))
        self.write(ui.style("  Decide at http://localhost:8000/dashboard (an approver signs in; not from here).", "grey"))

    def cmd_ask(self, args):
        if len(args) < 2:
            raise ValueError("/ask <role> <question>, e.g. /ask clinician What is our cancellation policy?")
        role, question = args[0], " ".join(args[1:])
        with ui.spinner("through the pipeline: safety, privacy, records, model, checks"):
            r = self.api.ask(question, role)
        if r.get("error"):
            self.write(f"  {ui.mark('bad')} {r['error']}")
            return
        g = r.get("guardrail")
        self.write("\n" + (r.get("answer") or "") + "\n")
        route = (r.get("route") or {}).get("model")
        self.write(ui.style(f"  {'stopped by ' + str(g.get('layer') or g.get('stage')) if g else 'answered'}"
                            + (f" · model {route}" if route else "") + f" · request {r.get('request_id', '')[:8]}", "grey"))

    def cmd_check(self, args):
        if not args:
            raise ValueError("which profile? e.g. /check clinic-oracle")
        with ui.spinner(f"checking {args[0]}'s record system"):
            res = pf.fhir_check(args[0], " ".join(args[1:]) or None)
        self.write(res["output"].rstrip())

    def cmd_new(self, args):
        from cli import builder as b
        self.write(ui.panel("new office profile", [
            "A few questions about the office. The local model drafts the rest from your answers.",
            "The profile keeps the platform's safety baseline and is tested before it is saved.",
            "Ctrl-C stops without saving."]))
        try:
            answers = b.Interview(ask=self.read, say=self.write).run()
        except (KeyboardInterrupt, EOFError):
            self.write(ui.style("  stopped; nothing was saved", "grey"))
            return
        ok, what = self.agent.available()
        drafter = b.Drafter(self.agent.model, self.agent.host, self.agent.http) if ok else None
        if not drafter:
            self.write(f"  {ui.mark('warn')} {what}: using your answers as they are")
        notes = []
        with ui.spinner(f"drafting with {self.agent.model}" if drafter else "building"):
            b.draft(answers, drafter, notes.append)
        for n in notes:
            self.write(ui.style(f"  ⎿ {n}", "grey"))
        for _ in range(4):
            path = b.generate(answers)
            passed, lines = b.check(path, answers)
            self.write(ui.panel(f"{answers.name}: checks", [f"{ui.mark('ok' if good else 'bad')} {t}" for good, t in lines]))
            missed = [d for d in answers.decisions for t in (d.tests or d.phrases[:2])
                      if any(not good and t in line for good, line in lines)]
            if passed or not missed or "loader refuses" in lines[0][1]:
                break
            d = missed[0]
            self.write(f"  {ui.mark('warn')} Not every request for \"{d.what}\" was refused.")
            more = b.Interview(self.read, self.write).lines("   Add phrases that appear in those questions")
            if not more:
                break
            d.phrases = list(dict.fromkeys([*d.phrases, *more]))
        if "loader refuses" in lines[0][1]:
            self.write(ui.style("  not saved: the platform would not load it", "grey"))
            return
        self.write(ui.panel(answers.name, [
            f"{answers.title} · for {answers.audience}" + (" · web search" if answers.web else ""),
            *(f"never decides: {d.what} → {d.who} ({len(d.phrases)} phrases)" for d in answers.decisions),
            f"{len(answers.knowledge)} approved notes · {len(answers.examples)} example questions",
            "baseline kept: crisis, warning signs, abuse, medication, diagnosis, legal, financial"]))
        prompt = (f"  Save it to profiles/{answers.name}? [y/N] " if passed else
                  f"  {ui.style('Some checks failed.', 'amber')} Type save to keep it anyway, anything else to stop: ")
        with ui.paused():
            reply = self.read(prompt).strip().lower()
        if reply not in (("y", "yes") if passed else ("save",)):
            self.write(ui.style("  not saved", "grey"))
            return
        target = b.save(path, answers.name)
        self.write(f"  {ui.mark('ok')} saved to profiles/{target.name}. Review its files (the notes and replies are "
                   f"drafts), then {ui.style('/use ' + target.name, 'cyan')} to run it.")

    def cmd_model(self, args):
        if args:
            self.agent.model = args[0]
        ok, what = self.agent.available()
        self.write(f"  {ui.mark('ok' if ok else 'warn')} {what}")

    def cmd_clear(self, args):
        self.agent.reset()
        self.write(ui.style("  the assistant forgot this conversation", "grey"))

    # --- the loop ------------------------------------------------------------------------------------

    def run(self) -> int:
        self.write(ui.banner("operator console · type /help, or ask in plain words"))
        self.screen()
        _readline(self)
        prompt = ui.style("gaai", "teal", "bold") + ui.style(" › ", "grey")
        while True:
            try:
                line = self.read(prompt)
            except EOFError:
                self.write("")
                return 0
            except KeyboardInterrupt:
                self.write(ui.style("  (Ctrl-D or /quit to leave)", "grey"))
                continue
            if not self.do(line):
                return 0


def _readline(shell: Shell) -> None:
    """History across sessions and tab completion of commands, profiles and services, if readline is there."""
    try:
        import readline
    except ImportError:  # pragma: no cover  (Windows without pyreadline)
        return
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    try:
        readline.read_history_file(HISTORY)
    except OSError:
        pass
    import atexit
    atexit.register(lambda: readline.write_history_file(HISTORY))
    words = list(COMMANDS) + [p["name"] for p in pf.profiles()] + [s.name for s in pf.services()]

    def complete(text, state):
        hits = [w for w in words if w.startswith(text)]
        return hits[state] if state < len(hits) else None
    readline.set_completer(complete)
    readline.set_completer_delims(" ")
    readline.parse_and_bind("bind ^I rl_complete" if "libedit" in (readline.__doc__ or "") else "tab: complete")
    os.environ.setdefault("GAAI_SHELL", "1")
