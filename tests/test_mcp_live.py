"""The MCP seams over a real connection: a tool server, the gateway's client, and the governed endpoint,
each started here on a free port. These are the tests that say a change of MCP library still works."""

import asyncio
import contextlib
import json
import socket
import threading
import time

import pytest
import uvicorn

from gateway import endpoint as ep
from gateway import gateway
from gateway.transport import mcp_session, tool_text
from mcp_servers.common.serve import tool_server
from profiles.loader import load_profile


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def running(app, port: int):
    """Serve an ASGI app on 127.0.0.1:port in a thread for the length of the block."""
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.02)
    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


@pytest.fixture(scope="module")
def tool_url():
    from mcp.server.mcpserver import Context
    mcp = tool_server("test-tools", free_port())

    @mcp.tool()
    def echo(query: str) -> dict:
        """Returns the query."""
        return {"results": [{"id": "r1", "title": "Echo", "text": query}]}

    @mcp.tool()
    async def talkative(query: str, ctx: Context) -> dict:
        """Reports its progress as it works."""
        await ctx.report_progress(1, None, f"working on {query}")
        return {"results": []}

    @mcp.tool()
    def broken(query: str) -> dict:
        """Always fails."""
        raise RuntimeError("the record system did not answer")

    host, port = mcp.listen
    with running(mcp.streamable_http_app(stateless_http=True, host=host), port) as url:
        yield url


def test_the_gateway_client_calls_a_tool_and_reads_its_result(tool_url):
    result = asyncio.run(gateway._call(tool_url, "echo", {"query": "hello"}))
    assert result == {"results": [{"id": "r1", "title": "Echo", "text": "hello"}]}


def test_a_tool_error_is_a_failure_and_its_internals_stay_on_the_server(tool_url):
    with pytest.raises(gateway.ToolFailed) as failure:
        asyncio.run(gateway._call(tool_url, "broken", {"query": "x"}))
    assert "broken" in str(failure.value) and "did not answer" not in str(failure.value)   # the library hides the cause


def test_progress_messages_reach_the_caller_while_the_tool_runs(tool_url):
    heard = []
    asyncio.run(gateway._call(tool_url, "talkative", {"query": "the chart"}, on_progress=heard.append))
    assert heard == ["working on the chart"]


def test_every_request_stands_alone(tool_url):
    """Stateless: two separate sessions, no shared session id, both answered."""
    async def twice():
        out = []
        for word in ("one", "two"):
            async with mcp_session(tool_url) as session:
                out.append(tool_text(await session.call_tool("echo", {"query": word}))[0])
        return out
    assert [json.loads(t)["results"][0]["text"] for t in asyncio.run(twice())] == ["one", "two"]


def test_a_server_that_is_not_there_fails_within_the_time_limit():
    started = time.time()
    with pytest.raises(Exception):  # noqa: B017, PT011 - the kind of failure is the library's; that it fails fast is ours
        asyncio.run(gateway._call(f"http://127.0.0.1:{free_port()}/mcp", "echo", {"query": "x"}, timeout=3))
    assert time.time() - started < 10


# ---- the governed endpoint, as an outside assistant sees it

class Introspector:
    def __init__(self, known):
        self.known = known

    def identify(self, token):
        return self.known.get(token)


class Recorder:
    def __init__(self):
        self.calls = []

    def handle(self, who, tool, arguments, request_id=None):
        self.calls.append((who.role, tool, arguments, request_id))
        return {"results": [{"id": "x1", "title": "T", "text": "ok"}]}


@pytest.fixture()
def governed(monkeypatch):
    from mcp import types
    profile = load_profile("clinic-oracle")
    catalogue = {name: types.Tool(name=name, description=name, input_schema={"type": "object", "properties": {"query": {"type": "string"}}})
                 for name in ("search_records", "medication_list")}
    monkeypatch.setattr(ep, "allowed_tools", lambda p, role: ["search_records", "medication_list"] if role == "clinician" else ["search_records"])
    monkeypatch.setattr(ep, "_audit", lambda topic, event: None)

    def identity(role):
        import inspect
        values = {"client_id": f"c-{role}", "name": role, "tenant": "clinic", "role": role, "location": "local",
                  "external_model_allowed": False}
        return ep.Identity(**{k: v for k, v in values.items() if k in inspect.signature(ep.Identity).parameters})
    recorder = Recorder()
    app = ep.build_app(profile, Introspector({"tok-clin": identity("clinician"), "tok-desk": identity("front_desk")}),
                       catalogue, recorder)
    with running(app, free_port()) as url:
        yield url, recorder


async def _tools_and_call(url, token, tool):
    async with mcp_session(url, headers={"Authorization": f"Bearer {token}"}) as session:
        names = [t.name for t in (await session.list_tools()).tools]
        text, failed = tool_text(await session.call_tool(tool, {"query": "q"})) if tool in names else ("", False)
        return names, text, failed


def test_each_caller_sees_only_its_roles_tools_and_calls_go_to_the_endpoint(governed):
    url, recorder = governed
    names, text, failed = asyncio.run(_tools_and_call(url, "tok-clin", "medication_list"))
    assert names == ["search_records", "medication_list"] and not failed and json.loads(text)["results"][0]["id"] == "x1"
    assert recorder.calls == [("clinician", "medication_list", {"query": "q"}, None)]
    names, _, _ = asyncio.run(_tools_and_call(url, "tok-desk", "medication_list"))
    assert names == ["search_records"] and len(recorder.calls) == 1            # not offered, so not called


def test_an_unknown_token_is_refused_before_any_mcp_message(governed):
    url, recorder = governed
    with pytest.raises(BaseException):  # noqa: B017, PT011 - a 401 surfaces as the library's connection error
        asyncio.run(_tools_and_call(url, "not-a-token", "search_records"))
    assert recorder.calls == []
