"""MCP gateway: checks policy, calls the tool, and audits every decision.

Which tools each agent may call comes from the active profile (agents section of
profiles/<name>/profile.yaml). The gateway loads that into OPA under
data.profiles.<name>; the policy (policies/mcp_authz.rego) denies everything else.
"""

import asyncio
import json

import requests
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from audit.chain import get_audit_chain
from profiles.loader import Profile, load_profile

OPA_URL = "http://localhost:8181"
OPA_DECISION = f"{OPA_URL}/v1/data/mcp/authz/allow"

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


def _ensure_policy_data(profile: Profile) -> None:
    """Load the profile's tool permissions into OPA if they are not there yet
    (for example after OPA restarted)."""
    url = f"{OPA_URL}/v1/data/profiles/{profile.name}"
    resp = requests.get(url, timeout=5)
    resp.raise_for_status()
    if resp.json().get("result") != profile.policy_data():
        requests.put(url, json=profile.policy_data(), timeout=5).raise_for_status()


def _is_allowed(profile: Profile, agent: str, tool: str) -> bool:
    _ensure_policy_data(profile)
    resp = requests.post(OPA_DECISION, json={
        "input": {"profile": profile.name, "agent": agent, "tool": tool},
    }, timeout=5)
    resp.raise_for_status()
    return resp.json().get("result", False) is True


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
