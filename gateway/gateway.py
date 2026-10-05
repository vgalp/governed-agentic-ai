"""MCP gateway: checks policy, calls the tool, and audits every decision."""

import asyncio
import json

import requests
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from audit.chain import get_audit_chain

OPA_URL = "http://localhost:8181/v1/data/mcp/authz/allow"
TOOL_SERVERS = {
    "search_knowledge": "http://127.0.0.1:8100/mcp",
}


class ToolCallDenied(Exception):
    pass


def _audit(event: dict) -> None:
    # Same chain as the calling agent's process, so tool calls and guardrail
    # decisions share one tamper-evident sequence.
    get_audit_chain("gateway").emit("audit.tool_calls", event, flush=True)


def _is_allowed(agent: str, tool: str) -> bool:
    resp = requests.post(OPA_URL, json={"input": {"agent": agent, "tool": tool}}, timeout=5)
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


def call_tool(agent: str, tool: str, args: dict, request_id: str) -> dict:
    allowed = tool in TOOL_SERVERS and _is_allowed(agent, tool)
    _audit({
        "request_id": request_id,
        "agent": agent,
        "tool": tool,
        "args": args,  # args contain masked text only
        "decision": "allow" if allowed else "deny",
    })
    if not allowed:
        raise ToolCallDenied(f"Agent '{agent}' may not call tool '{tool}'")
    return asyncio.run(_call(TOOL_SERVERS[tool], tool, args))