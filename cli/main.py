"""gaai: the platform's command line (ADR 033).

    ./gaai                                  the console (start screen, commands, local assistant)
    ./gaai status [--json]                  services, profile, health, audit, approvals
    ./gaai start|restart [--profile NAME]   start the platform (dev.sh), or switch profile
    ./gaai stop
    ./gaai profiles                         the profiles and their record systems
    ./gaai health | audit [--all] | approvals
    ./gaai logs SERVICE [-n 60]
    ./gaai check PROFILE [--patient "Wilm Smart 1990-09-15"]
    ./gaai ask --role clinician "What is our cancellation policy?"     through the governed pipeline
    ./gaai chat "is everything running?"    one question to the local assistant (Qwen via Ollama)

Exit status: 0 when what was asked for is fine (status: every service running; audit: intact).
"""

import argparse
import sys

from cli import platform as pf
from cli import ui


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="gaai", description="The governed agentic AI platform's command line.",
                                 epilog="Without a command: the console.")
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("status", help="services, profile, health, audit, approvals")
    s.add_argument("--json", action="store_true")
    for name in ("start", "restart"):
        p = sub.add_parser(name, help=f"{name} the platform")
        p.add_argument("--profile")
        p.add_argument("--yes", "-y", action="store_true", help="do not ask")
    p = sub.add_parser("stop", help="stop the platform's services")
    p.add_argument("--yes", "-y", action="store_true")
    sub.add_parser("profiles", help="the profiles and where their records come from")
    sub.add_parser("health", help="the health report")
    a = sub.add_parser("audit", help="verify the audit chain")
    a.add_argument("--all", action="store_true", help="every profile's chains")
    sub.add_parser("approvals", help="changes waiting for an approver")
    lg = sub.add_parser("logs", help="the end of a service's log")
    lg.add_argument("service")
    lg.add_argument("-n", type=int, default=40)
    c = sub.add_parser("check", help="check a profile's record system")
    c.add_argument("profile")
    c.add_argument("--patient")
    k = sub.add_parser("ask", help="send a question through the governed pipeline")
    k.add_argument("question")
    k.add_argument("--role", required=True)
    k.add_argument("--json", action="store_true")
    ch = sub.add_parser("chat", help="one question to the local assistant")
    ch.add_argument("question")
    ch.add_argument("--model")
    args = ap.parse_args(argv)

    from cli.shell import Shell
    shell = Shell()
    if args.cmd is None:
        return shell.run()
    if args.cmd == "status":
        snap = pf.snapshot(shell.api)
        if args.json:
            print(pf.to_json(snap))
        else:
            print(ui.panel("status", __import__("cli.shell", fromlist=["status_lines"]).status_lines(
                snap, shell.agent.available())))
        return 0 if snap["running"] == snap["total"] else 1
    if args.cmd in ("start", "restart", "stop"):
        if args.yes:
            shell.confirm = lambda tool, a: True
        res = pf.dev(args.cmd, getattr(args, "profile", None), on_line=print) if args.yes else None
        if res is None:
            shell.do(f"/{args.cmd} {getattr(args, 'profile', None) or ''}")
            return 0
        return 0 if res["ok"] else 1
    if args.cmd == "audit":
        a = shell.api.audit("all" if args.all else "profile")
        shell.do("/audit all" if args.all else "/audit")
        return 0 if a and a.get("intact") else 1
    if args.cmd == "logs":
        shell.do(f"/logs {args.service} {args.n}")
        return 0
    if args.cmd == "check":
        res = pf.fhir_check(args.profile, args.patient)
        print(res["output"].rstrip())
        return 0 if res["ok"] else 1
    if args.cmd == "ask":
        r = shell.api.ask(args.question, args.role)
        if args.json:
            print(pf.to_json(r))
        else:
            print(r.get("error") or r.get("answer") or "")
        return 1 if r.get("error") else 0
    if args.cmd == "chat":
        if args.model:
            shell.agent.model = args.model
        ok, what = shell.agent.available()
        if not ok:
            print(what, file=sys.stderr)
            return 1
        print(shell.agent.ask(args.question))
        return 0
    shell.do("/" + args.cmd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
