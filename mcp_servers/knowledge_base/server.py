"""MCP server exposing a search tool over the active profile's approved knowledge base
(profiles/<name>/data/knowledge_base.json)."""

import json
import re

from mcp.server.fastmcp import FastMCP

from profiles.loader import load_profile

PROFILE = load_profile()
ENTRIES = json.loads(PROFILE.knowledge_base.read_text(encoding="utf-8"))

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
    print(f"Knowledge base for profile {PROFILE.name}: {len(ENTRIES)} entries")
    mcp.run(transport="streamable-http")