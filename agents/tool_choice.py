"""Which of an agent's tools a question gets, and the reply when its role may not use one.

A tool with `when` (tools.yaml) is for some questions only: it is called when the question matches
its rules.yaml pattern, and then alone. Every other tool is called for the remaining questions.
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
