"""Governed MCP endpoint: lets assistants outside this pipeline (Kiro, Claude Desktop, an
organization's own app) use the profile's tools under the same rules as the pipeline's own
agents (ADR 015).

    PROFILE_DIR=/path/to/profile uv run python -m gateway.endpoint

Every request:
  1. Who is calling. The caller sends a bearer token; the endpoint asks the organization's own
     system (outside.yaml auth.url, "token introspection") who it is: client, tenant, role,
     and whether the client runs locally or at an outside AI service. The tenant and role come
     from that answer, never from the request. No answer, no access (HTTP 401).
  2. Which tools. A caller sees and may call only the tools its role may use (roles.yaml),
     among those granted to the outside agent (profile.yaml agents).
  3. The checks. Text in a tool's arguments goes through the same input checks as a question
     (rules, then the classifier): a crisis is never passed on as a query, and a decision that
     must stay with a person is declined. The caller gets the profile's fixed reply instead.
  4. The call. Through the gateway (gateway.py): OPA decides, the tenant is set from step 1,
     records of other tenants are dropped, and the call is audited with the client's identity.
  5. What leaves. Results for clients at an outside AI service are masked (outside.yaml
     mask_results), and data the profile marks as never leaving (routing.never_external_labels)
     is refused to them altogether.

Everything is written to the same tamper-evident audit chain as the rest of the pipeline.
Limit: the endpoint governs what an outside assistant does with tools and data. It does not
see the rest of that assistant's conversation.
"""

import asyncio
import contextlib
import hashlib
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass

import anyio
import requests
from mcp import types
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

from gateway import gateway
from guardrails.check import check_text
from profiles.loader import Profile, load_profile

HIDDEN_PARAMS = ("tenant_id", "allowed_classes")   # set by the gateway, never by a caller


@dataclass(frozen=True)
class Identity:
    client_id: str
    name: str
    tenant: str | None
    role: str | None
    location: str            # local: runs on the organization's machines · external: an outside AI service


class Unauthorized(Exception):
    pass


# --- 1. who is calling ------------------------------------------------------------------------

class Introspector:
    """Asks the organization's own system who a token belongs to (RFC 7662 style), and trusts the
    answer for a short time. Fails closed: no answer, or anything malformed, means no access."""

    def __init__(self, url: str, secret: str, cache_seconds: int, post=requests.post, clock=time.monotonic):
        self.url, self.secret, self.cache_seconds = url, secret, cache_seconds
        self._post, self._clock = post, clock
        self._cache: dict[str, tuple[Identity | None, float]] = {}
        self._lock = threading.Lock()

    def identify(self, token: str | None) -> Identity | None:
        if not token:
            return None
        key = hashlib.sha256(token.encode()).hexdigest()      # the token itself is never kept
        now = self._clock()
        with self._lock:
            hit = self._cache.get(key)
            if hit and hit[1] > now:
                return hit[0]
        identity = self._ask(token)
        with self._lock:
            self._cache[key] = (identity, now + (self.cache_seconds if identity else 2))
        return identity

    def _ask(self, token: str) -> Identity | None:
        try:
            resp = self._post(self.url, json={"token": token},
                              headers={"X-Introspect-Secret": self.secret}, timeout=5)
            if resp.status_code != 200:
                return None
            d = resp.json()
        except Exception:  # noqa: BLE001 - any failure to verify means no access
            return None
        if not isinstance(d, dict) or d.get("active") is not True or not d.get("client_id"):
            return None
        location = d.get("location", "external")
        return Identity(str(d["client_id"]), str(d.get("name", d["client_id"])),
                        str(d["tenant_id"]) if d.get("tenant_id") else None,
                        str(d["role"]) if d.get("role") else None,
                        location if location in ("local", "external") else "external")


def bearer(headers) -> str | None:
    value = headers.get("authorization") or ""
    if not value.lower().startswith("bearer "):
        return None
    return value[7:].strip() or None


# --- 2. which tools ---------------------------------------------------------------------------

def allowed_tools(profile: Profile, role: str | None) -> list[str]:
    """Tools this caller may see and call: the outside agent's grant, narrowed by the role."""
    granted = list(profile.agents[profile.outside.agent])
    if not profile.roles:
        return granted
    r = profile.roles.get(role or "")
    return [t for t in granted if r and t in r.tools]


def strip_hidden(schema: dict) -> dict:
    """A tool's input schema without the parameters only the gateway may set."""
    schema = json.loads(json.dumps(schema or {"type": "object"}))
    for p in HIDDEN_PARAMS:
        (schema.get("properties") or {}).pop(p, None)
    if "required" in schema:
        schema["required"] = [r for r in schema["required"] if r not in HIDDEN_PARAMS]
    return schema


async def upstream_catalogue(profile: Profile) -> dict[str, types.Tool]:
    """The definitions of the outside agent's tools, read from their own MCP servers."""
    wanted = set(profile.agents[profile.outside.agent])
    found: dict[str, types.Tool] = {}
    for server in sorted({profile.tools[t].server for t in wanted}):
        async with streamablehttp_client(server) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                for t in (await session.list_tools()).tools:
                    if t.name in wanted and profile.tools[t.name].server == server:
                        found[t.name] = types.Tool(name=t.name, description=profile.tools[t.name].description
                                                   or t.description, inputSchema=strip_hidden(t.inputSchema))
    missing = wanted - set(found)
    if missing:
        raise SystemExit(f"Tool server(s) do not offer: {', '.join(sorted(missing))}")
    return found


# --- 3-5. one call ----------------------------------------------------------------------------

def texts(value) -> list[str]:
    """Every piece of text in a tool's arguments."""
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, dict):
        return [t for v in value.values() for t in texts(v)]
    if isinstance(value, (list, tuple)):
        return [t for v in value for t in texts(v)]
    return []


def mask_values(value, mask_text):
    """The same structure with every text masked."""
    if isinstance(value, str):
        return mask_text(value)
    if isinstance(value, dict):
        return {k: mask_values(v, mask_text) for k, v in value.items()}
    if isinstance(value, list):
        return [mask_values(v, mask_text) for v in value]
    return value


def masks_results(profile: Profile, who: Identity) -> bool:
    mode = profile.outside.mask_results
    return mode == "always" or (mode == "external" and who.location == "external")


class Endpoint:
    """The checks and the call, separate from the MCP plumbing so they can be tested directly."""

    def __init__(self, profile: Profile, check=check_text, call=gateway.call_tool, mask=None, audit=None):
        self.profile = profile
        self._check, self._call = check, call
        self._mask = mask
        self._audit = audit or _audit

    def mask_text(self, text: str) -> str:
        if self._mask is None:
            from privacy.masking import mask                # loads the analyzer once, on first use
            self._mask = lambda t: mask(t, self.profile)[0]
        return self._mask(text)

    def _event(self, who: Identity, request_id: str, **fields) -> dict:
        return {"request_id": request_id, "profile": self.profile.name, "agent": self.profile.outside.agent,
                "client": who.client_id, "tenant": who.tenant, "role": who.role, **fields}

    def handle(self, who: Identity, tool: str, arguments: dict | None) -> dict:
        p = self.profile
        request_id = f"out-{uuid.uuid4()}"
        args = {k: v for k, v in (arguments or {}).items() if k not in HIDDEN_PARAMS}

        if tool not in allowed_tools(p, who.role):
            self._audit("audit.tool_calls", self._event(who, request_id, tool=tool, decision="deny",
                                                        reason="tool not available to this role"))
            return {"error": "not_allowed", "message": f"Tool '{tool}' is not available to your role."}

        found = texts(args)
        if found:
            label, decision, layer = self._check("\n".join(found), p)
            self._audit("audit.guardrails", self._event(
                who, request_id, stage="tool_input", tool=tool, layer=layer, allowed=decision.allowed,
                category=decision.category, reason=decision.reason))
            if not decision.allowed:
                category = decision.category or "unavailable"
                reply = p.response(category, who.role) if category in p.responses else decision.response
                return {"stopped": label, "category": category, "reply": reply,
                        **({"hand_over": True} if label == "crisis" else {})}

        if who.location == "external" and p.tools[tool].data_label in p.routing["never_external_labels"]:
            self._audit("audit.tool_calls", self._event(who, request_id, tool=tool, decision="deny",
                                                        reason="data never leaves the organization"))
            return {"error": "not_allowed",
                    "message": "This data stays inside the organization and can't be sent to an outside assistant."}

        audit_args = mask_values(args, self.mask_text) if found else dict(args)
        try:
            result = self._call(p.outside.agent, tool, args, request_id, p, audit_args=audit_args,
                                role=who.role, tenant=who.tenant, client=who.client_id)
        except gateway.ToolCallDenied:
            return {"error": "not_allowed", "message": f"Tool '{tool}' is not available to you."}
        except Exception as e:  # noqa: BLE001 - the caller gets a plain error, never a stack trace
            return {"error": "tool_failed", "message": f"The tool could not run ({type(e).__name__})."}
        return mask_values(result, self.mask_text) if masks_results(p, who) else result


def _audit(topic: str, event: dict) -> None:
    from audit.chain import get_audit_chain
    get_audit_chain("endpoint").emit(topic, event, flush=True)


# --- the MCP server ---------------------------------------------------------------------------

def build_app(profile: Profile, introspector: Introspector, catalogue: dict[str, types.Tool],
              endpoint: Endpoint):
    """A Starlette app serving the governed endpoint at /mcp."""
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    server = Server("governed-endpoint")

    def caller() -> Identity:
        request = server.request_context.request
        who = introspector.identify(bearer(request.headers)) if request is not None else None
        if who is None:
            raise Unauthorized("unknown or revoked token")
        return who

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        who = caller()
        return [catalogue[t] for t in allowed_tools(profile, who.role) if t in catalogue]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
        who = caller()
        result = await anyio.to_thread.run_sync(endpoint.handle, who, name, arguments)
        return [types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False, default=str))]

    manager = StreamableHTTPSessionManager(app=server, stateless=True)

    class GovernedMCP:
        """ASGI app at exactly /mcp (no redirect to /mcp/, which some clients do not follow)."""

        async def __call__(self, scope, receive, send):
            await mcp_asgi(scope, receive, send)

    async def mcp_asgi(scope, receive, send):
        # Refuse unknown callers before any MCP message is read.
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        token = bearer(headers)
        if await anyio.to_thread.run_sync(introspector.identify, token) is None:
            await anyio.to_thread.run_sync(_audit, "audit.tool_calls", {
                "request_id": f"out-{uuid.uuid4()}", "profile": profile.name, "agent": profile.outside.agent,
                "decision": "deny", "reason": "unauthenticated" if token else "no token"})
            response = JSONResponse({"error": "unauthorized"}, status_code=401,
                                    headers={"WWW-Authenticate": 'Bearer realm="governed-endpoint"'})
            await response(scope, receive, send)
            return
        await manager.handle_request(scope, receive, send)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        async with manager.run():
            yield

    return Starlette(routes=[Route("/mcp", endpoint=GovernedMCP(), methods=["GET", "POST", "DELETE"])],
                     lifespan=lifespan)


def main() -> None:
    import uvicorn
    profile = load_profile()
    if profile.outside is None:
        raise SystemExit(f"Profile '{profile.name}' has no outside.yaml: nothing to serve.")
    o = profile.outside
    secret = os.environ.get(o.secret_env)
    if not secret:
        raise SystemExit(f"Set {o.secret_env} (the shared secret for the token check).")
    catalogue = asyncio.run(upstream_catalogue(profile))
    app = build_app(profile, Introspector(o.introspect_url, secret, o.cache_seconds), catalogue,
                    Endpoint(profile))
    print(f"Governed MCP endpoint for profile {profile.name}: http://{o.host}:{o.port}/mcp "
          f"({len(catalogue)} tools, callers checked at {o.introspect_url})")
    uvicorn.run(app, host=o.host, port=o.port, log_level="warning")


if __name__ == "__main__":
    main()
