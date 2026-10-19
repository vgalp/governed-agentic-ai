"""MCP gateway: checks policy, calls the tool, and audits every decision.

Which tools exist, and where each one's MCP server runs, comes from the active
profile's tools.yaml; which tools each agent may call comes from its profile.yaml
(agents). The gateway loads both into OPA under data.profiles.<name>; the policy
(policies/mcp_authz.rego) denies everything else, including any tool that changes
something unless the caller is the agent that carries out approved changes.

In a profile with roles, the requesting role must also be allowed to use the tool,
and for role-filtered tools the gateway asks OPA which data classes the role may see
(policies/roles.rego), passes them to the tool, and drops any returned record that
contains another class. It fails closed: a record without data classes is dropped.

Tenant-scoped tools (tools.yaml tenant_scoped: true) hold several customers' data. The
caller's tenant comes from who the caller is (the outside endpoint's auth check), never
from the request: the gateway overwrites any tenant_id in the arguments, OPA refuses the
call without a tenant, and any returned record for another tenant, or with none, is dropped.
"""

import asyncio
import json

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from audit.chain import get_audit_chain
from gateway.opa import decide
from guardrails.injection import check_result
from profiles.loader import Profile, load_profile


class ToolCallDenied(Exception):
    pass


def _audit(event: dict) -> None:
    # Same chain as the calling agent's process, so tool calls and guardrail
    # decisions share one tamper-evident sequence.
    get_audit_chain("gateway").emit("audit.tool_calls", event, flush=True)


def _is_allowed(profile: Profile, agent: str, tool: str, role: str | None,
                tenant: str | None = None) -> bool:
    result = decide(profile, "mcp/authz/allow",
                    {"profile": profile.name, "agent": agent, "tool": tool, "role": role,
                     "tenant": tenant or ""})
    return result is True


def tenant_filter(results: list[dict], tenant: str) -> tuple[list[dict], int]:
    """Keep only records of this tenant. Returns (kept, dropped). Fails closed: a record
    without a tenant_id is dropped."""
    kept = [r for r in results if r.get("tenant_id") == tenant]
    return kept, len(results) - len(kept)


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
              role: str | None = None, tenant: str | None = None,
              client: str | None = None) -> dict:
    """Call a tool if policy allows it. audit_args is what the audit records instead of
    args, for tools that receive real identifiers: the audit only ever holds masked text.

    tenant and client come from the outside endpoint's auth check (who is calling), never
    from the request. For a tenant-scoped tool the tenant is required and enforced."""
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
    if client is not None:
        event["client"] = client
    scoped = tool in profile.tools and profile.tools[tool].tenant_scoped
    if scoped or tenant is not None:
        event["tenant"] = tenant
    if tool not in profile.tools or (scoped and not tenant):
        allowed = False
    elif tenant is not None:
        allowed = _is_allowed(profile, agent, tool, role, tenant=tenant)
    else:
        allowed = _is_allowed(profile, agent, tool, role)
    if not allowed:
        _audit({**event, "decision": "deny"})
        raise ToolCallDenied(f"Role '{role}' / agent '{agent}' may not call tool '{tool}' "
                             f"in profile '{profile.name}'")

    if scoped:
        args = {**args, "tenant_id": tenant}       # always the caller's tenant, whatever was sent
        event["args"].pop("tenant_id", None)
    filtered = tool in profile.role_filtered_tools
    classes = allowed_classes(profile, role) if filtered else None
    if filtered:
        args = {**args, "allowed_classes": classes}
        event["args"]["allowed_classes"] = classes
    try:
        result = asyncio.run(_call(profile.tools[tool].server, tool, args))
    except Exception as e:
        _audit({**event, "decision": "allow", "error": type(e).__name__})
        raise
    if filtered:
        kept, dropped = filter_results(result.get("results", []), classes)
        result = {**result, "results": kept}
        event["dropped_by_gateway"] = dropped      # should always be 0; >0 means the tool over-returned
    if scoped:
        kept, dropped = tenant_filter(result.get("results", []), tenant)
        result = {**result, "results": kept}
        event["dropped_other_tenant"] = dropped    # should always be 0; >0 means the tool leaked
    # Text from the tool's sources may try to instruct the AI (prompt injection): withheld here,
    # before any model or outside assistant reads it (guardrails/injection.py, ADR 016).
    result, injection = check_result(result, tool)
    if injection:
        event["injection_withheld"] = json.dumps(result, default=str).count("[withheld: this text from")
        event["injection_kinds"] = sorted(set(injection))
    event["returned"] = len(result.get("results", []))
    _audit({**event, "decision": "allow"})
    return result