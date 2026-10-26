"""Which of an agent's tools a question gets, and the reply when its role may not use one.

A tool with `when` (tools.yaml) is for some questions only: it is called when the question matches
its rules.yaml pattern, and then alone. Every other tool is called for the remaining questions.

`route` is the whole choice in one place, as the planner makes it: a request for a change first
(actions.yaml), then a triggered tool, else the general tools. It is what the tool-use accuracy
evaluation scores (eval/run_tool_use.py).
"""

from profiles.loader import Profile


def tools_to_call(profile: Profile, agent: str, message: str) -> tuple[list[str], list[str]]:
    """(tools to call, of those the ones the question triggered)."""
    granted = profile.agents.get(agent, ())
    triggered = [t for t in granted if profile.tools[t].when and profile.tools[t].when.search(message)]
    return (triggered or [t for t in granted if not profile.tools[t].when]), triggered


def tool_denied_reply(profile: Profile, tool: str, role: str | None) -> str:
    """Fixed reply when a question is for a tool the role may not use: what it is, and who may."""
    who = [r.title for r in profile.roles.values() if tool in r.tools]
    return profile.response("tool_denied", role).format(
        tool=profile.tools[tool].description or tool,
        role=profile.roles[role].title if role in profile.roles else role,
        roles=", ".join(who) or "no role in this organization")


def route(profile: Profile, agent: str, role: str | None, message: str) -> dict:
    """Where the planner sends a question that passed the input checks, and whether the role may go there.

    path      "change:<action>" (a proposal for a person to approve), "tool:<name>" (a tool the
              question triggered), or "general" (the tools every other question gets)
    allowed   whether the role may take that path: the same rules OPA applies
              (policies/mcp_authz.rego: the tool is granted to the agent and listed for the role;
              policies/actions.rego: the role may propose that change)
    tools     the tools that would be called (for general: those the role may use)
    """
    from agents import actions                       # here, so tool_choice stays importable on its own
    roles = profile.roles
    action = actions.detect(profile, message) if profile.actions else None
    if action:
        allowed = not roles or role in action.propose_roles
        return {"path": f"change:{action.name}", "allowed": allowed, "tools": [action.tool] if allowed else []}
    to_call, triggered = tools_to_call(profile, agent, message)
    may = (lambda t: True) if not roles else (lambda t: role in roles and t in roles[role].tools)
    if triggered:
        tool = triggered[0]
        return {"path": f"tool:{tool}", "allowed": may(tool), "tools": [tool] if may(tool) else []}
    usable = [t for t in to_call if may(t)]
    return {"path": "general", "allowed": bool(usable), "tools": usable}
