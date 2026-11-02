# 022: Governed web research, a second model as judge, and Qwen as the answering model

Status: Accepted · Date: 2026-10-31

## Context
The everyday assistant (ADR 021) answers from a 7B model's own knowledge. For anything current
(a help line's text option, a rule that changed this year) that is stale or invented. Chatbots
that search the web usually do it unsafely: the user's text goes to a search engine as typed, any
page is read, the page can instruct the model, and the summary cannot be checked.

Separately, a side-by-side on this profile showed Qwen 2.5 7B answering and summarising better
than Mistral 7B, and the medication review (ADR 017) already set the pattern this needs: retrieve
from a source, quote it with IDs, let no model reword the facts unchecked.

## Decision
### 1. Web research is a read tool, with no model in it
`mcp_servers/web_research` searches (DuckDuckGo's HTML endpoint by default, or a SearXNG the
office runs), fetches a few pages, and returns them as **snapshots**: ID (`web-` plus a hash of
the page text), URL, title, time fetched, SHA-256 and an excerpt. It retrieves; it has no opinion.
- **Only the masked question is sent.** The tool does not receive identifiers, so the gateway gives
  it masked text; the tool also refuses a query that still holds a placeholder, an email, a phone
  number or a long digit run. Nothing personal reaches a search engine.
- **Only allowed sites are read.** `web_research.yaml` lists allowed and denied domains, pages per
  question, characters per page and timeouts. The everyday profile allows `.gov`, `.edu`, WHO, NIH,
  CDC, FDA, 988lifeline.org, Mayo Clinic, Wikipedia and the Canadian government sites, and denies
  social media. An office edits the list; empty means any site not denied. HTTPS only.
- **It runs only for questions that ask for it** (`when` in tools.yaml: "search the web", "latest",
  "current", "recent", "news about"), and then alone, like the medication review.
- **Pages are untrusted.** They pass through the gateway's injection check (ADR 016), which withholds
  any text that looks like instructions to the AI before the model sees it.
- **The audit records what was read:** the snapshot IDs, URLs and hashes go with the tool call, and
  the answer cites the IDs, so the grounding check holds and a reader can see exactly which page
  version an answer came from.

### 2. A second model judges answers written from web pages
`guardrails/faithfulness.py` is a new output layer, after rules, grounding and the classifier. A
profile that sets a `faithfulness` block names a **judge model**, which must not be an answering
model (the loader refuses it), and the tools whose results it judges. For those answers the judge
reads the sources and the answer and returns JSON: the claims the sources do not support. Any
unsupported claim withholds the answer with the `unfaithful` reply; the sources are still listed.
- The judge is a different model family from the writer: Meta's Llama 3.1 8B judges Qwen's answers.
  The same model must not grade its own work.
- `strict: true` (the default) withholds when the judge cannot be reached or does not answer in
  JSON. An office may set `strict: false` to let such answers through.
- The trace and audit record the judge's name, verdict and the number of unsupported claims, never
  the claims' text.
- It is opt-in and scoped to listed tools. Records and approved notes keep the grounding check only;
  the judge is for text the office did not approve.

### 3. Qwen 2.5 7B answers in the everyday profile
`general-assistant` uses `qwen2.5:7b` as its local model, chosen after a side-by-side on this
profile's questions. It is a profile setting, not a platform default:
- The layer is vendor-neutral. Each profile names its models; the clinic and county profiles stay
  on Mistral until they are compared the same way.
- Qwen is Alibaba's model. A U.S. office may prefer a U.S. open model (gpt-oss-20b, Gemma, Granite,
  Phi) or a hosted one; the profile is the place to say so, and nothing else changes.
- Qwen already generates the guard classifier's training data and judges the chatbot comparison.
  Where Qwen answers, the judge is Llama, so no evaluation has Qwen on both sides.

## Amendment (2026-10-31, the same evening): an invented citation got through
In live use the question *"search the wbe who is hte president of usa"* (typos) was answered
*"…Joe Biden. This information was fetched from [W1234567890] on [2023-10-01]"*: a stale fact from
the model's memory, with a citation and a date that never existed. Four gaps let it through:
1. The typos matched no trigger, so no search ran and Qwen answered from memory. The trigger now
   also matches `search` followed by any short word, `… of` after an office (president, prime
   minister, mayor, CEO), `today`, prices and "is … still …".
2. The everyday profile never set `cite_sources`/`source_id_pattern`, so the model was shown pages
   without IDs, told to cite IDs, and the grounding check never ran. Both are now set
   (`gen-###`, `web-xxxxxxxx`).
3. The grounding check only looked for IDs in the profile's own format. It now also blocks any
   bracketed identifier-like token the model was not given (`[W1234567890]`, `[2023-10-01]`,
   `[doc 12]`); masking placeholders such as `[PERSON_1]` are not citations.
4. When research is the question's tool and finds nothing, no model answers. `tools.yaml` names
   a fixed reply (`on_empty: no_web_pages`) that says what was searched and why nothing came back
   (no results · results not on an allowed site · pages unreadable · network). The judge also now
   runs whenever a judged tool was *called*, not only when it returned pages.

Also: the tool reports how many results the search returned (shown in the trace), and when
DuckDuckGo's HTML endpoint returns no links (it serves a bot check to scripts), the tool tries
DuckDuckGo lite and then Wikipedia's own search, the latter only if wikipedia.org is allowed.

Lesson for the paper: every check must be defined by what it *blocks*, not by what a well-behaved
model would write. A citation check that trusts the citation format is not a check.

## Consequences
- The everyday assistant can answer "what are the current 988 text and chat options" from the
  Lifeline's own page, with the page cited and dated, and the answer checked by a second model.
- Two more local models run (Qwen and Llama 3.1); `scripts/dev.sh` now reads the active profile's
  models and checks they are pulled.
- A research question takes longer: a search, up to three fetches, the answer, and the judge.
- Search quality is DuckDuckGo's HTML results, which is modest. An office that needs better can
  point `search: searxng` at its own instance.
- The judge is a 7B–8B model and will sometimes be wrong both ways. Withholding is the safe
  failure: the person still gets the pages. How often it withholds correctly is a question for the
  chatbot comparison, where "invented facts" is already a measure.
- Nothing about crisis, decisions, masking or roles changes. A research question that is also a
  decision ("latest dose for…") is still stopped before any search runs.

## Amendment (2026-11-01): the search reports its work as it happens
The research tool sends an MCP log message at each step (search sent, result count, each site
skipped as not allowed, each page read with its snapshot ID, fallbacks tried). The gateway, the
only path to any tool, passes them to the planner as it waits, and the planner writes each one
to the trace. The chat bubble shows them live in place of "Checking…", and the staff dashboard
lists them under the tool's lane. The messages hold hosts, counts and IDs, never page text, so
nothing new enters the audit. Any tool can report this way; the records tools do not yet.

## Amendment (2026-11-01): two modes for the judge
An 8B judge is wrong both ways, and in live use it withheld plain answers ("what is One Piece",
from two Wikipedia pages). The judge prompt now grades specific facts only (names, dates, numbers,
office-holders, prices, rules, quotes), says why each is rejected ("not in sources" or
"contradicted: …"), and paraphrase is explicitly fine. The profile chooses what a finding does:
- `mode: withhold` (default; clinic and county): any unsupported fact withholds the answer.
- `mode: warn` (the everyday profile): a fact the pages **contradict** withholds the answer (the
  stale-memory case: "Trudeau" against a page that says Carney); a fact the pages merely do not
  mention sends the answer with a fixed warning (`not_confirmed`) and the trace records it.
The judge is never removed: the contradiction check is what caught "Joe Biden, sworn in 2023".
How often each mode is right is a question for the chatbot comparison set, which should get a
"research" section: questions with a known-good answer, counted as correct pass / correct withhold
/ false withhold.
