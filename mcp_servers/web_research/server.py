"""MCP server: governed web research (ADR 022). For a question that asks for current information,
search the web, fetch a few pages from allowed sites, and return them as sources with snapshots.

No model runs here. The tool retrieves; the planner's model writes the answer from what it
returns, under the usual checks (rules, grounding, classifier) and the faithfulness judge.

What leaves the machine: the MASKED question only. The planner restores place names before it
sends the query (tools.yaml `keeps: [LOCATION]`): "rules in Ontario" needs the place, and a place
is not a personal detail. People's names and contact details stay masked, and a query that still
carries any placeholder, or that masking could not clean, is refused here as well.

Each page returned is a snapshot: URL, title, time fetched, SHA-256 of the text, and an excerpt.
The answer cites snapshots by ID (web-<8 hex>), so the grounding check holds and the audit shows
exactly what was read. Pages are untrusted text: the gateway's injection check (ADR 016) withholds
anything in them that looks like instructions to the AI, before the model sees it.

Settings come from the profile's web_research.yaml:
  search           duckduckgo (HTML endpoint, no key) · searxng (set url)
  allow            domains that may be fetched (suffix match); empty: any site not in deny
  deny             domains never fetched
  max_pages        pages fetched per question (default 3)
  max_chars        characters kept per page (default 6000)
  timeout_seconds  per request (default 10)
"""

import asyncio
import hashlib
import html
import ipaddress
import re
import socket
import sys
import urllib.parse
from datetime import datetime, timezone
from html.parser import HTMLParser

import requests
import yaml

from mcp_servers.common.serve import serve
from profiles.loader import Profile, load_profile

UA = "governed-agentic-ai/web-research (+https://github.com/vgalp/governed-agentic-ai)"
MASK_TOKEN = re.compile(r"\[[A-Z_]+_\d+\]|<[A-Z_]+_?\d*>")  # any placeholder left in the query
DDG = "https://html.duckduckgo.com/html/"
DDG_LITE = "https://lite.duckduckgo.com/lite/"  # tried when the HTML endpoint returns no links
WIKI_SEARCH = "https://en.wikipedia.org/w/api.php"  # tried last, only if wikipedia.org is allowed
DEFAULTS = {
    "search": "duckduckgo",
    "allow": [],
    "deny": [],
    "max_pages": 3,
    "max_chars": 6000,
    "timeout_seconds": 10,
    "max_results": 8,
}
KNOWN_KEYS = set(DEFAULTS) | {"url"}
MAX_REDIRECTS = 3


def load_settings(profile: Profile) -> dict | None:
    path = profile.path / "web_research.yaml"
    if not path.exists():
        return None
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    unknown = sorted(set(raw) - KNOWN_KEYS)
    if unknown:                                  # a misspelled "alow:" must not silently allow every site
        raise ValueError(f"{path.name}: unknown key(s) {', '.join(unknown)} (misspelled?)")
    if not raw.get("allow"):
        raise ValueError(f"{path.name}: allow: must list the sites that may be read (an empty list would allow every site)")
    s = {**DEFAULTS, **raw}
    s["allow"] = [d.lower().lstrip(".") for d in s["allow"] or []]
    s["deny"] = [d.lower().lstrip(".") for d in s["deny"] or []]
    if s["search"] not in ("duckduckgo", "searxng"):
        raise ValueError(f"{path.name}: search must be duckduckgo or searxng")
    if s["search"] == "searxng" and not s.get("url"):
        raise ValueError(f"{path.name}: searxng needs url")
    return s


# --- the parts that are pure, tested without network ------------------------------------------


def domain_allowed(url: str, settings: dict) -> bool:
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if not host or urllib.parse.urlparse(url).scheme != "https":
        return False
    hit = lambda d: host == d or host.endswith("." + d)  # noqa: E731
    if any(hit(d) for d in settings["deny"]):
        return False
    return not settings["allow"] or any(hit(d) for d in settings["allow"])


TRIGGER_WORDS = re.compile(
    r"^\s*(?:(?:please|can you|could you)\s+)?(?:do a |run a )?(?:web search|search the web|"
    r"search online|look (?:it |this )?up online|google|search)\s*(?:for|and (?:see|tell me|find out)|:)?\s*",
    re.IGNORECASE,
)


def clean_query(query: str) -> str | None:
    """The query to send, or None if it still holds a personal-detail placeholder or looks like one
    slipped past masking (an email, a phone number, a long digit run). The words that asked for a
    search ("search the web for") are dropped: they are for the assistant, not the search engine."""
    q = " ".join(query.split())
    if MASK_TOKEN.search(q):
        return None
    q = TRIGGER_WORDS.sub("", q, count=1).strip(" ?.!,") or q
    if re.search(r"[\w.+-]+@[\w-]+\.[\w.]+|\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b|\b\d{9,}\b", q):
        return None
    return q[:300] or None


class _Text(HTMLParser):
    """Visible text of a page, with scripts, styles and navigation dropped."""

    SKIP = {"script", "style", "noscript", "nav", "footer", "header", "aside", "svg", "form"}

    def __init__(self):
        super().__init__()
        self.parts, self.title, self._skip, self._in_title = [], "", 0, False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        if tag == "title":
            self._in_title = True
        if tag in ("p", "br", "li", "h1", "h2", "h3", "h4", "tr", "div"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip:
            self.parts.append(data)


def extract(html_text: str, max_chars: int) -> tuple[str, str]:
    """(title, readable text) from an HTML page."""
    p = _Text()
    try:
        p.feed(html_text)
    except Exception:  # noqa: BLE001  a broken page is just less text
        pass
    text = html.unescape("".join(p.parts))
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    return " ".join(p.title.split())[:200], text[:max_chars]


def parse_ddg(page: str) -> list[dict]:
    """Result links from DuckDuckGo's HTML or lite endpoint: [{url, title}]."""
    out = []
    for m in re.finditer(r"<a\b([^>]*)>(.*?)</a>", page, re.S):
        attrs = m.group(1)
        if not re.search(r'class="[^"]*\b(result__a|result-link)\b', attrs):
            continue
        h = re.search(r'href="([^"]+)"', attrs)
        if not h:
            continue
        href = html.unescape(h.group(1))
        # DDG wraps targets as /l/?uddg=<encoded url>
        q = urllib.parse.parse_qs(urllib.parse.urlparse(href).query).get("uddg")
        url = q[0] if q else href
        title = re.sub(r"<[^>]+>", "", m.group(2))
        out.append({"url": url, "title": html.unescape(" ".join(title.split()))})
    return out


def snapshot_id(url: str, text: str) -> str:
    return "web-" + hashlib.sha256((url + "\n" + text).encode("utf-8")).hexdigest()[:8]


def build_results(pages: list[dict], max_chars: int) -> list[dict]:
    """Snapshots the planner can give the model: id, title, text (an excerpt) and the audit fields."""
    out = []
    for p in pages:
        text = p["text"][:max_chars]
        sid = snapshot_id(p["url"], text)
        out.append(
            {
                "id": sid,
                "title": p["title"] or p["url"],
                "url": p["url"],
                "fetched_at": p["fetched_at"],
                "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "text": text,
                "status": "web page, fetched for this question; not approved content",
            }
        )
    return out


# --- network -----------------------------------------------------------------------------------


def search(query: str, settings: dict, session=None, progress=None) -> list[dict]:
    """Result links for the query. progress(message) is told what is tried, for the trace."""
    say = progress or (lambda m: None)
    s = session or requests.Session()
    if settings["search"] == "searxng":
        r = s.get(
            settings["url"].rstrip("/") + "/search",
            params={"q": query, "format": "json"},
            headers={"User-Agent": UA},
            timeout=settings["timeout_seconds"],
        )
        r.raise_for_status()
        return [{"url": x["url"], "title": x.get("title", "")} for x in r.json().get("results", [])]
    headers = {"User-Agent": UA, "Accept": "text/html"}
    r = s.post(DDG, data={"q": query}, headers=headers, timeout=settings["timeout_seconds"])
    r.raise_for_status()
    hits = parse_ddg(r.text)
    if not hits:  # the HTML endpoint sometimes answers a script with a bot check page
        say("DuckDuckGo returned no result links; trying its lite endpoint")
        try:
            r = s.get(DDG_LITE, params={"q": query}, headers=headers, timeout=settings["timeout_seconds"])
            if r.status_code == 200:
                hits = parse_ddg(r.text)
        except requests.RequestException:
            pass
    usable = [h for h in hits[: settings["max_results"]] if domain_allowed(h["url"], settings)]
    if not usable and domain_allowed("https://en.wikipedia.org/", settings):
        say(
            "No result on an allowed site; trying Wikipedia's own search"
            if hits
            else "Still nothing; trying Wikipedia's own search"
        )
        hits = wikipedia_search(query, settings, s) + hits  # first, so they are among those read
    return hits


def wikipedia_search(query: str, settings: dict, s) -> list[dict]:
    """Wikipedia's own search, as a last resort: no key, stable, and the site is on the allowed list."""
    try:
        r = s.get(
            WIKI_SEARCH,
            params={"action": "opensearch", "search": query, "limit": 5, "format": "json"},
            headers={"User-Agent": UA},
            timeout=settings["timeout_seconds"],
        )
        if r.status_code != 200:
            return []
        data = r.json()
        return [{"url": u, "title": t} for t, u in zip(data[1], data[3])]
    except (requests.RequestException, ValueError, IndexError, TypeError):
        return []


def host_is_private(host: str) -> bool:
    """True if the host is, or resolves to, an address on this machine or the local network
    (loopback, private, link-local, reserved). A page must never be read from there: an allowed
    site's redirect could otherwise point the reader at Kafka UI or an intranet page. A host that
    does not resolve is not private here; the request then fails on its own."""
    try:
        addrs = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            addrs = [ipaddress.ip_address(a[4][0].split("%")[0]) for a in socket.getaddrinfo(host, 443)]
        except (socket.gaierror, UnicodeError, ValueError):
            return False
    return any(a.is_private or a.is_loopback or a.is_link_local or a.is_reserved or a.is_unspecified for a in addrs)


def fetch(url: str, settings: dict, session=None) -> dict | None:
    """Read one page. Redirects are followed by hand, at most MAX_REDIRECTS, and every hop is checked
    again (https, the allow-list, not a local address): an open redirect on an allowed site must
    not lead anywhere else. The page is recorded under the URL it was actually read from."""
    s = session or requests.Session()
    try:
        for _ in range(MAX_REDIRECTS + 1):
            host = urllib.parse.urlparse(url).hostname or ""
            if not domain_allowed(url, settings) or host_is_private(host):
                return None
            r = s.get(url, headers={"User-Agent": UA}, timeout=settings["timeout_seconds"], stream=True,
                      allow_redirects=False)
            if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                url = urllib.parse.urljoin(url, r.headers["location"])
                continue
            break
        else:
            return None                          # too many redirects
        if r.status_code != 200 or "html" not in (r.headers.get("content-type") or ""):
            return None
        body = r.raw.read(400_000, decode_content=True).decode(r.encoding or "utf-8", errors="replace")
    except requests.RequestException:
        return None
    title, text = extract(body, settings["max_chars"])
    if len(text) < 200:
        return None
    return {"url": url, "title": title, "text": text, "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def research(query: str, settings: dict | None, session=None, progress=None) -> dict:
    """The tool: search, fetch allowed pages, return snapshots. Never raises for a dead network.
    progress(message) is called at each step (search sent, results, each page read or skipped), so
    the caller can show the work as it happens; the messages hold hosts and counts, never page text."""
    say = progress or (lambda m: None)
    if settings is None:
        return {"results": [], "note": "web research is not set up for this profile"}
    q = clean_query(query)
    if q is None:
        say("The question still holds a personal detail: not sent to the web")
        return {"results": [], "note": "the question holds personal details, so it was not sent to the web"}
    say(f'Searching {settings["search"]} for "{q}"')
    try:
        hits = search(q, settings, session, progress)
    except (requests.RequestException, ValueError) as e:
        say(f"The search did not answer ({type(e).__name__})")
        return {"results": [], "note": f"the web search did not answer: {type(e).__name__}"}
    considered = hits[: settings["max_results"]]
    say(f"{len(hits)} result(s)" + (f"; checking the first {len(considered)} against the allowed sites" if hits else ""))
    pages, skipped, skipped_hosts = [], 0, []
    for h in considered:
        if len(pages) >= settings["max_pages"]:
            break
        host = urllib.parse.urlparse(h["url"]).hostname or h["url"]
        if not domain_allowed(h["url"], settings):
            skipped += 1
            skipped_hosts.append(host)
            say(f"Skipped {host}: not on the allowed list")
            continue
        say(f"Reading {host}")
        page = fetch(h["url"], settings, session)
        if page:
            page["title"] = page["title"] or h["title"]
            pages.append(page)
            say(
                f"Read {host}: {len(page['text']):,} characters, "
                f"snapshot {snapshot_id(page['url'], page['text'][: settings['max_chars']])}"
            )
        else:
            say(f"Could not read {host} (not HTML, too short, or no answer)")
    out = {
        "results": build_results(pages, settings["max_chars"]),
        "query_sent": q,
        "hits": len(hits),
        "searched": settings["search"],
        "skipped_not_allowed": skipped,
    }
    if not pages:
        out["note"] = (
            "the search returned no results"
            if not hits
            else "none of the results was on an allowed site"
            if skipped == len(hits[: settings["max_results"]])
            else "no page from an allowed site could be read"
        )
    return out


if __name__ == "__main__":
    from mcp.server.fastmcp import Context, FastMCP

    PROFILE = load_profile()
    try:
        SETTINGS = load_settings(PROFILE)
    except ValueError as e:
        sys.exit(f"web research: {e}")
    mcp = FastMCP("web_research", host="127.0.0.1", port=8105)

    @mcp.tool()
    async def web_research(query: str, ctx: Context) -> dict:
        """Search the web for the question and return a few pages from allowed sites, as dated
        snapshots with IDs to cite. Retrieval only: no model, no opinion. Reports each step
        (search sent, results, pages read or skipped) as MCP log messages, so the caller can
        show the work as it happens."""
        loop = asyncio.get_running_loop()

        def progress(message: str) -> None:  # called from the worker thread
            try:
                asyncio.run_coroutine_threadsafe(ctx.info(message), loop).result(timeout=5)
            except Exception:  # noqa: BLE001  a lost progress message must not fail the search
                pass

        return await asyncio.to_thread(research, query, SETTINGS, None, progress)

    if SETTINGS is None:
        print(f"Web research for profile {PROFILE.name}: not set up (no web_research.yaml)")
    else:
        print(
            f"Web research for profile {PROFILE.name}: {SETTINGS['search']}, "
            f"{'any site' if not SETTINGS['allow'] else str(len(SETTINGS['allow'])) + ' allowed domain(s)'}, "
            f"{SETTINGS['max_pages']} page(s) per question"
        )
    serve(mcp)   # plain HTTP, or mutual TLS (ADR 030)
