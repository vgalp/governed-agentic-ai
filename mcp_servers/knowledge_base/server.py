"""MCP server exposing a search tool over the approved knowledge base."""

import json
import re
from pathlib import Path

from mcp.server.fastmcp import FastMCP

ENTRIES = json.loads((Path(__file__).parent / "entries.json").read_text())

mcp = FastMCP("knowledge-base", host="127.0.0.1", port=8100)

STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "can", "do", "for",
    "from", "how", "i", "in", "is", "it", "me", "my", "of", "on", "or", "s",
    "should", "so", "that", "the", "this", "to", "was", "what", "when", "where",
    "which", "who", "why", "will", "with", "you", "your",
}

def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]+", text.lower()) if w not in STOP_WORDS and len(w) > 2}


@mcp.tool()
def search_knowledge(query: str, limit: int = 3) -> dict:
    """Search the approved knowledge base and return the best-matching entries."""
    query_words = _words(query)
    scored = []
    for entry in ENTRIES:
        entry_words = _words(entry["title"] + " " + entry["text"]) | set(entry["tags"])
        score = len(query_words & entry_words)
        if score > 0:
            scored.append((score, entry))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    results = [
        {"id": e["id"], "title": e["title"], "text": e["text"], "status": e["status"]}
        for _, e in scored[:limit]
    ]
    return {"results": results}


if __name__ == "__main__":
    mcp.run(transport="streamable-http")