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

In a profile that tracks sensitive categories of health information (ADR 038), each record
from a role-filtered tool is also marked with its categories, and records in a category the
role may not see are withheld. A tool's own answer text is withheld with them. The audit
records the categories and the counts.

Tenant-scoped tools (tools.yaml tenant_scoped: true) hold several customers' data. The
caller's tenant comes from who the caller is (the outside endpoint's auth check), never
from the request: the gateway overwrites any tenant_id in the arguments, OPA refuses the
call without a tenant, and any returned record for another tenant, or with none, is dropped.
"""

import asyncio
import json
from collections import Counter
from datetime import timedelta

from mcp import ClientSession

from audit.chain import get_audit_chain
from gateway.opa import decide
from gateway.transport import open_mcp
from guardrails.injection import check_result
from profiles.loader import Profile, load_profile


class ToolFailed(Exception):
    """The tool ran but returned an error or something that is not a result (its text is the message).
    The caller treats it as 'no results' and keeps running: one failing tool must not stop the service."""


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


def allowed_sensitive(profile: Profile, role: str | None) -> list[str]:
    result = decide(profile, "roles/allowed_sensitive", {"profile": profile.name, "role": role})
    return list(result or [])


def tag_sensitive(results: list[dict], profile: Profile) -> tuple[list[dict], Counter]:
    """Each record with the sensitive categories this office tracks (ADR 038): what its title and text
    show, added to what the tool found in the record's codes and labels. A tool can add a category,
    never remove one. Returns the records and how many are in each category."""
    settings = profile.sensitivity
    detector = settings.detector()
    tagged, counts = [], Counter()
    for r in results:
        claimed = r.get("sensitivity") if isinstance(r.get("sensitivity"), list) else []
        found = sorted(set(settings.tracked(detector.of_record(r))) | {c for c in claimed if c in settings.categories})
        tagged.append({**r, "sensitivity": found})
        counts.update(found)
    return tagged, counts


def filter_sensitive(results: list[dict], allowed: list[str]) -> tuple[list[dict], Counter]:
    """Keep only records whose sensitive categories the role may all see. Returns the records kept and,
    by category, how many were withheld (a record in two categories counts under each it lacks)."""
    ok = set(allowed)
    kept, withheld = [], Counter()
    for r in results:
        missing = [c for c in r.get("sensitivity", []) if c not in ok]
        if missing:
            withheld.update(missing)
        else:
            kept.append(r)
    return kept, withheld


DEFAULT_TOOL_TIMEOUT = 30.0   # seconds; a tool may set its own in tools.yaml (timeout:)


async def _call(server_url: str, tool: str, args: dict, on_progress=None,
                timeout: float | None = None) -> dict:
    """on_progress(message): a tool's MCP log messages, as it works (web research reports each
    page read); the planner shows them in the trace. Text only, never results.

    timeout bounds the whole call: a tool server that accepts the connection and then hangs
    fails the request (ToolFailed) instead of holding the planner until Kafka evicts it."""
    limit = timeout or DEFAULT_TOOL_TIMEOUT
    try:
        return await asyncio.wait_for(_call_once(server_url, tool, args, on_progress, limit), limit)
    except TimeoutError:
        raise ToolFailed(f"{tool} did not answer within {limit:g} s") from None


async def _call_once(server_url: str, tool: str, args: dict, on_progress, limit: float) -> dict:
    async def logged(params) -> None:
        if on_progress is not None and params.level in ("info", "notice", "warning"):
            on_progress(str(params.data)[:300])
    async with open_mcp(server_url, timeout=min(limit, 30.0), sse_read_timeout=limit) as (read, write, _):
        async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=limit),
                                 logging_callback=logged if on_progress else None) as session:
            await session.initialize()
            result = await session.call_tool(tool, args)
            text = result.content[0].text if result.content and hasattr(result.content[0], "text") else ""
            if getattr(result, "isError", False):              # the server raised: its message, not JSON
                raise ToolFailed(f"{tool}: {text[:300] or 'error with no message'}")
            if getattr(result, "structuredContent", None):
                return result.structuredContent
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                raise ToolFailed(f"{tool} returned something that is not a result: {text[:300]!r}") from None
            if not isinstance(parsed, dict):
                raise ToolFailed(f"{tool} returned {type(parsed).__name__}, not a result")
            return parsed


def call_tool(agent: str, tool: str, args: dict, request_id: str,
              profile: Profile | None = None, audit_args: dict | None = None,
              role: str | None = None, tenant: str | None = None,
              client: str | None = None, on_progress=None, launch: str | None = None) -> dict:
    """Call a tool if policy allows it. audit_args is what the audit records instead of
    args, for tools that receive real identifiers: the audit only ever holds masked text.
    on_progress(message) receives the tool's own progress messages while it runs.

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
    args.pop("launch_ref", None)                   # only ever from the launched session, never from a caller
    if launch and filtered:
        # Launched from the EHR (ADR 035): the record tools read as that clinician, on that chart. The
        # audit notes it; the reference itself is not written (it finds the encrypted session).
        args = {**args, "launch_ref": launch}
        event["launched"] = True
    try:
        result = asyncio.run(_call(profile.tools[tool].server, tool, args, on_progress,
                                 profile.tools[tool].timeout))
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
    if filtered and profile.sensitivity:
        # Sensitive categories (ADR 038): every record is marked with its categories, and a role sees
        # only the categories policy gives it. The audit records categories and counts, never values.
        tagged, found = tag_sensitive(result.get("results", []), profile)
        result = {**result, "results": tagged}
        event["sensitive_categories"] = dict(sorted(found.items()))
        if profile.roles and found:                # nothing marked: nothing to ask policy about
            kept, withheld = filter_sensitive(tagged, allowed_sensitive(profile, role))
            if withheld:
                # A tool's own answer text was written from records now withheld: it cannot be shown
                # in part (a medication review that leaves a medication out is worse than none).
                had_answer = "answer" in result
                result = {k: v for k, v in result.items() if k != "answer"}
                result.update(results=kept, withheld_sensitive=len(tagged) - len(kept), answer_withheld=had_answer)
                event["withheld_sensitive"] = dict(sorted(withheld.items()))
    # Text from the tool's sources may try to instruct the AI (prompt injection): withheld here,
    # before any model or outside assistant reads it (guardrails/injection.py, ADR 016).
    result, injection = check_result(result, tool)
    if injection:
        event["injection_withheld"] = json.dumps(result, default=str).count("[withheld: this text from")
        event["injection_kinds"] = sorted(set(injection))
    event["returned"] = len(result.get("results", []))
    _audit({**event, "decision": "allow"})
    return result
