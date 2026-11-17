# 041: The current MCP library, stateless requests, and two seams

Status: Accepted · Date: 2026-11-16 · Tested over real connections in the test suite; not yet run with the full stack

## Context
The project pinned the MCP library below version 2 (ADR 001). Since then the protocol was revised
(the 2026-07-28 specification): requests stand alone with no session to keep, the HTTP+SSE transport
and protocol-level logging are deprecated, and sign-in aligns with OAuth 2.1. Epic's Connectors and
OpenAI's Epic integration both use MCP. A gateway that speaks only the older protocol is harder to
put behind either.

Version 2 of the library renames or removes most of what the code used: the server class, the client
transport, the low-level server's decorators, and the field names of results.

## Decision
- **Use version 2** (`mcp>=2.3,<3`).
- **Two seams, so the library is named in two files.**
  - Servers: `mcp_servers/common/serve.py`. `tool_server(name, port)` makes a tool server and
    `serve()` runs it. Every tool server uses both.
  - Clients: `gateway/transport.py`. `mcp_session(url, ...)` gives a ready session (over mutual TLS
    where that is on, with a caller's headers) and `tool_text(result)` reads a tool's answer. The
    gateway, the governed endpoint and the check client use them.
- **Stateless.** Tool servers and the governed endpoint answer each request alone. The endpoint reads
  the caller's token on every request, as it did. Nothing ends a session on close, because there is none.
- **Streamable HTTP only.** No HTTP+SSE.
- **Progress, not log messages.** A tool that reports its work (web research) sends MCP progress
  messages; the gateway reads them from the call. Protocol-level logging is deprecated in the new
  specification.
- **Live tests** (`tests/test_mcp_live.py`): a tool server, the gateway's client and the governed
  endpoint are started on free ports and called over HTTP. They check a result, a failing tool,
  progress messages, two independent requests, a server that is not there, the tools each role is
  offered, and an unknown token. A future library change is judged by these.

## Consequences
- **A tool's unexpected error no longer carries its message to the caller.** The library answers
  "Error executing tool <name>" and keeps the cause on the server, in its log. That is the safer
  default for a record tool; the tools here return their own messages in their results and do not
  rely on exceptions.
- **`uv.lock` must be regenerated** (`uv lock --upgrade-package mcp`), and the container image rebuilt.
- **The test-only shim for this library is gone**: the suite runs against the real library.
- **Not done here:** the specification's sign-in (OAuth 2.1, enterprise-managed authorization). The
  endpoint still asks an identity system about each token. That is the next change.
- **Not verified:** the whole stack running on this version (policy engine, queue, models, a record
  system). The tests cover the MCP paths; a run on a developer's machine covers the rest.
