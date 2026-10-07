"""MCP gateway: checks policy, calls the tool, and audits every decision.

Which tools each agent may call comes from the active profile (agents section of
profiles/<name>/profile.yaml). The gateway loads that into OPA under
data.profiles.<name>; the policy (policies/mcp_authz.rego) denies everything else.
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
}


class ToolCallDenied(Exception):
    pass


def _audit(event: dict) -> None:
    # Same chain as the calling agent's process, so tool calls and guardrail
    # decisions share one tamper-evident sequence.
    get_audit_chain("gateway").emit("audit.tool_calls", event, flush=True)


def _is_allowed(profile: Profile, agent: str, tool: str) -> bool:
    result = decide(profile, "mcp/authz/allow", {"profile": profile.name, "agent": agent, "tool": tool})
    return result is True


async def _call(server_url: str, tool: str, args: dict) -> dict:
    async with streamablehttp_client(server_url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool, args)
            if getattr(result, "structuredContent", None):
                return result.structuredContent
            return json.loads(result.content[0].text)


def call_tool(agent: str, tool: str, args: dict, request_id: str,
              profile: Profile | None = None) -> dict:
    profile = profile or load_profile()
    allowed = tool in TOOL_SERVERS and _is_allowed(profile, agent, tool)
    _audit({
        "request_id": request_id,
        "profile": profile.name,
        "agent": agent,
        "tool": tool,
        "args": args,  # args contain masked text only
        "decision": "allow" if allowed else "deny",
    })
    if not allowed:
        raise ToolCallDenied(f"Agent '{agent}' may not call tool '{tool}' in profile '{profile.name}'")
    return asyncio.run(_call(TOOL_SERVERS[tool], tool, args))
