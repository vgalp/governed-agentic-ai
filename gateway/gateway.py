"""MCP gateway: checks policy, calls the tool, and audits every decision.

Which tools each agent may call comes from the active profile (agents section of
profiles/<name>/profile.yaml). The gateway loads that into OPA under
data.profiles.<name>; the policy (policies/mcp_authz.rego) denies everything else.

In a profile with roles, the requesting role must also be allowed to use the tool,
and for role-filtered tools the gateway asks OPA which data classes the role may see
(policies/roles.rego), passes them to the tool, and drops any returned record that
contains another class. It fails closed: a record without data classes is dropped.
"""

import asyncio
import json

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from audit.chain import get_audit_chain
from gateway.opa import decide
from profiles.loader import Profile, load_profile

# Tools this deployment can reach. A profile can only grant tools listed here.
TOOL_SERVERS = {
    "search_knowledge": "http://127.0.0.1:8100/mcp",
    "search_records": "http://127.0.0.1:8101/mcp",
    "reschedule_appointment": "http://127.0.0.1:8102/mcp",   # changes; executor agent only
}


class ToolCallDenied(Exception):
    pass


def _audit(event: dict) -> None:
    # Same chain as the calling agent's process, so tool calls and guardrail
    # decisions share one tamper-evident sequence.
    get_audit_chain("gateway").emit("audit.tool_calls", event, flush=True)


def _is_allowed(profile: Profile, agent: str, tool: str, role: str | None) -> bool:
    result = decide(profile, "mcp/authz/allow",
                    {"profile": profile.name, "agent": agent, "tool": tool, "role": role})
    return result is True


def allowed_classes(profile: Profile, role: str | None) -> list[str]:
    result = decide(profile, "roles/allowed_classes", {"profile": profile.name, "role": role})
    return list(result or [])


def filter_results(results: list[dict], allowed: list[str]) -> tuple[list[dict], int]:
    """Keep only records whose data classes are all allowed. Returns (kept, dropped)."""
    ok = set(allowed)
    kept = [r for r in results if r.get("data_classes") and set(r["data_classes"]) <= ok]
    return kept, len(results) - len(kept)


async def _call(server_url: str, tool: str, args: dict) -> dict:
    async with streamablehttp_client(server_url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool, args)
            if getattr(result, "structuredContent", None):
                return result.structuredContent
            return json.loads(result.content[0].text)


def call_tool(agent: str, tool: str, args: dict, request_id: str,
              profile: Profile | None = None, audit_args: dict | None = None,
              role: str | None = None) -> dict:
    """Call a tool if policy allows it. audit_args is what the audit records instead of
    args, for tools that receive real identifiers: the audit only ever holds masked text."""
    profile = profile or load_profile()
    if audit_args is None and tool in profile.identifier_tools:
        raise ValueError(f"{tool} receives identifiers: pass masked audit_args")
    event = {
        "request_id": request_id,
        "profile": profile.name,
        "agent": agent,
        "role": role,
        "tool": tool,
        "args": dict(audit_args if audit_args is not None else args),   # masked text only
    }
    allowed = tool in TOOL_SERVERS and _is_allowed(profile, agent, tool, role)
    if not allowed:
        _audit({**event, "decision": "deny"})
        raise ToolCallDenied(f"Role '{role}' / agent '{agent}' may not call tool '{tool}' "
                             f"in profile '{profile.name}'")

    filtered = tool in profile.role_filtered_tools
    classes = allowed_classes(profile, role) if filtered else None
    if filtered:
        args = {**args, "allowed_classes": classes}
        event["args"]["allowed_classes"] = classes
    try:
        result = asyncio.run(_call(TOOL_SERVERS[tool], tool, args))
    except Exception as e:
        _audit({**event, "decision": "allow", "error": type(e).__name__})
        raise
    if filtered:
        kept, dropped = filter_results(result.get("results", []), classes)
        result = {**result, "results": kept}
        event["dropped_by_gateway"] = dropped      # should always be 0; >0 means the tool over-returned
    event["returned"] = len(result.get("results", []))
    _audit({**event, "decision": "allow"})
    return result