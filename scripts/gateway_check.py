"""Call the governed gateway as an outside assistant would, and show what came back (ADR 039).

    export GATEWAY_TOKEN=<the token `python -m ops.dev_identity add` printed>
    uv run python scripts/gateway_check.py
    uv run python scripts/gateway_check.py --tool search_records --query "Wilm Smart, born 1990-09-15"
    uv run python scripts/gateway_check.py --url https://gateway.example.org/mcp

It connects over MCP with the bearer token, lists the tools this caller's role may use, calls one, and
prints how many records came back, how many were withheld, and the record IDs. Record text is printed
only with --show-text. The token is read from the environment, so it is not left in the shell history.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

DEFAULT_URL = "http://127.0.0.1:8110/mcp"
DEFAULT_QUERY = "What medications is Wilm Smart (born 1990-09-15) taking?"   # an Oracle Health sandbox test patient


def summary(result: dict, show_text: bool) -> list[str]:
    """What a person needs to see of one tool result. IDs and counts; text only when asked."""
    if "error" in result:                       # refused, or the tool could not run
        return [f"the gateway answered: {result['error']}: {result.get('message', '')}".rstrip(": ")]
    if "stopped" in result:                     # the request itself was stopped by the checks
        return [f"the request was stopped by the checks ({result['stopped']}: {result.get('category')}); "
                "the caller gets this fixed reply:", str(result.get("reply", ""))]
    if not isinstance(result.get("results"), list):
        return [f"an answer in a shape this check does not know, with: {', '.join(sorted(result)) or 'nothing'}"]
    records = result["results"]
    lines = [f"records returned: {len(records)}"]
    if result.get("withheld_sensitive"):
        lines.append(f"records withheld for this role (protected categories): {result['withheld_sensitive']}")
    if result.get("answer_withheld"):
        lines.append("the tool's own answer was withheld with them")
    if result.get("note"):
        lines.append(f"note: {result['note']}")
    for r in records:
        marked = f" [{', '.join(r['sensitivity'])}]" if r.get("sensitivity") else ""
        lines.append(f"  {r.get('id')}: {r.get('title')}{marked}" + (f" | {r.get('text')}" if show_text else ""))
    if result.get("answer") and show_text:
        lines += ["answer:", result["answer"]]
    elif result.get("answer"):
        lines.append(f"answer: {len(result['answer'])} characters (--show-text to print it)")
    return lines


async def run(url: str, token: str, tool: str, query: str, show_text: bool) -> int:
    async with streamablehttp_client(url, headers={"Authorization": f"Bearer {token}"}) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = [t.name for t in (await session.list_tools()).tools]
            print(f"tools this caller may use: {', '.join(tools) or 'none'}")
            if tool not in tools:
                print(f"{tool} is not one of them: nothing called.")
                return 1
            reply = await session.call_tool(tool, {"query": query})
            text = reply.content[0].text if reply.content and hasattr(reply.content[0], "text") else ""
            if getattr(reply, "isError", False):
                print(f"the gateway refused or failed: {text[:300]}")
                return 1
            try:
                result = json.loads(text)
            except json.JSONDecodeError:
                print(f"reply: {text[:300]}")                      # a fixed reply (a stopped question) is plain text
                return 0
            print("\n".join(summary(result, show_text)) if isinstance(result, dict) else str(result)[:300])
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Call the governed gateway as an outside assistant would.")
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--tool", default="medication_list")
    ap.add_argument("--query", default=DEFAULT_QUERY)
    ap.add_argument("--show-text", action="store_true", help="print record text and the tool's answer")
    args = ap.parse_args(argv)
    token = os.environ.get("GATEWAY_TOKEN")
    if not token:
        print("Set GATEWAY_TOKEN to the caller's token (python -m ops.dev_identity add ... prints one).")
        return 2
    try:
        return asyncio.run(run(args.url, token, args.tool, args.query, args.show_text))
    except Exception as e:  # noqa: BLE001  a connection problem is a message, not a traceback
        cause = e.exceptions[0] if isinstance(e, BaseExceptionGroup) and e.exceptions else e
        print(f"Could not use the gateway at {args.url}: {type(cause).__name__}: {str(cause)[:200]}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
