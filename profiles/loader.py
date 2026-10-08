"""Load and validate a deployment profile.

A profile is a folder under profiles/ that holds everything specific to one use
case: the model, system prompt, guardrail rules, fixed replies, classifier policy,
privacy settings, knowledge base and which tools each agent may call. The pipeline
code is shared, so a new use case is a new folder, not new code.

The active profile comes from the PROFILE environment variable (default:
adhd-assistant). A profile that is incomplete or inconsistent fails at startup
with a clear message, never halfway through a request.
"""

import os
import re
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path

import yaml

PROFILES_DIR = Path(__file__).resolve().parent
DEFAULT_PROFILE = "adhd-assistant"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
SCOPES = {"text", "sentence"}
PROVIDERS = {"ollama", "openai", "gemini", "anthropic", "openai_compatible", "mock"}
HOSTED_PROVIDERS = {"openai", "gemini", "anthropic"}   # third-party services: always external
LOCATIONS = {"local", "external"}
SEND_EXTERNAL_WHEN = {"no_personal_info", "masked"}
LOCAL_INPUT = {"masked", "original"}
FIELD_TYPES = {"source", "datetime", "text"}
ACTION_RESPONSES = ("action_pending", "action_denied", "action_unclear")


class ProfileError(Exception):
    """The profile is missing something or contradicts itself."""


@dataclass(frozen=True)
class Rule:
    category: str
    reason: str
    all: tuple[re.Pattern, ...]
    any: tuple[re.Pattern, ...]
    scope: str = "text"
    none: tuple[re.Pattern, ...] = ()       # patterns that must NOT match


@dataclass(frozen=True)
class ClassifierCategory:
    code: str        # code in the classifier policy, e.g. "S1"
    name: str        # human-readable, used in audit reasons
    category: str    # guardrail category, e.g. "crisis"


@dataclass(frozen=True)
class ClassifierConfig:
    model: str
    policy: str
    categories: tuple[ClassifierCategory, ...]   # priority order
    default_category: str


@dataclass(frozen=True)
class Role:
    title: str
    tools: tuple[str, ...]          # tools this role may use
    data_classes: tuple[str, ...]   # kinds of record data this role may see
    responses: dict[str, str]       # fixed replies worded for this role (optional overrides)


@dataclass(frozen=True)
class Action:
    """A change the assistant may PROPOSE. A person must approve it before it is made."""
    name: str
    title: str
    description: str
    intent: re.Pattern               # a request matching this starts a proposal
    tool: str                        # the tool that makes the change
    agent: str                       # the only agent that may call that tool (after approval)
    fields: dict[str, dict]          # what the proposal must contain: type, description, pattern
    propose_roles: tuple[str, ...]   # who may ask for it
    approve_roles: tuple[str, ...]   # who may approve it (never the person who asked)
    expires_minutes: int             # unapproved after this: expired, nothing is changed
    allowed_times: dict | None       # optional: weekdays and hours a datetime field must fall in


@dataclass(frozen=True)
class Profile:
    name: str
    title: str
    description: str
    version: int
    path: Path
    models: dict[str, dict]                     # key -> provider, name, location, ...
    routing: dict
    tool_data_labels: dict[str, str]            # tool -> label of the data it returns
    system_prompt: str
    responses: dict[str, str]
    input_rules: tuple[Rule, ...]
    output_rules: tuple[Rule, ...]
    classifier: ClassifierConfig
    privacy_entities: tuple[str, ...]
    privacy_allow_list: tuple[str, ...]
    knowledge_base: Path
    records_db: Path | None                     # SQLite records database, if the profile has one
    identifier_tools: tuple[str, ...]           # tools that may receive real names (local only)
    cite_sources: bool                          # show source IDs to the model so it can cite them
    agents: dict[str, tuple[str, ...]]          # agent -> tools it may call
    roles: dict[str, Role]                      # empty: the profile has no roles
    data_classes: dict[str, str]                # data class -> description
    role_filtered_tools: tuple[str, ...]        # tools whose results are filtered by role
    examples: tuple[str, ...]                   # example questions for the chat page
    classifier_exemptions: tuple[Rule, ...]     # narrow cases where an input classifier flag is overridden
    source_id_pattern: re.Pattern | None        # what a cited source ID looks like (grounding check)
    actions: dict[str, Action] = field(default_factory=dict)   # changes the assistant may propose (need approval)

    def response(self, category: str, role: str | None = None) -> str:
        """The fixed reply for a category, worded for the role if the role has its own."""
        r = self.roles.get(role) if role else None
        if r and category in r.responses:
            return r.responses[category]
        return self.responses[category]

    @property
    def local_model(self) -> dict:
        return self.models[self.routing["local_model"]]

    @property
    def external_model(self) -> dict | None:
        key = self.routing.get("external_model")
        return self.models[key] if key else None

    def policy_data(self) -> dict:
        """What OPA needs for this profile: tool access, routing and roles."""
        r = self.routing
        data = {
            "agents": {a: {"tools": list(t)} for a, t in self.agents.items()},
            "routing": {
                "external_allowed": r["external_allowed"],
                "send_external_when": r["send_external_when"],
                "never_external_entities": list(r["never_external_entities"]),
                "never_external_labels": list(r["never_external_labels"]),
            },
        }
        if self.roles:
            data["roles"] = {k: {"tools": list(v.tools), "data_classes": list(v.data_classes)}
                             for k, v in self.roles.items()}
        if self.actions:
            data["actions"] = {k: {"propose_roles": list(a.propose_roles),
                                   "approve_roles": list(a.approve_roles),
                                   **({"allowed_times": a.allowed_times} if a.allowed_times else {})}
                               for k, a in self.actions.items()}
        return data


# --- helpers ------------------------------------------------------------------

def _require(d: dict, key: str, where: str):
    if key not in d or d[key] in (None, ""):
        raise ProfileError(f"{where}: missing '{key}'")
    return d[key]


def _file(base: Path, rel: str, where: str) -> Path:
    p = (base / rel).resolve()
    if base.resolve() not in p.parents:
        raise ProfileError(f"{where}: '{rel}' points outside the profile folder")
    if not p.is_file():
        raise ProfileError(f"{where}: file not found: {rel}")
    return p


def _yaml(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ProfileError(f"{path.name}: invalid YAML: {e}") from e
    if not isinstance(data, dict):
        raise ProfileError(f"{path.name}: expected a mapping at the top level")
    return data


def read_prompt(path: Path) -> str:
    """Lines within a paragraph are joined with spaces; blank lines separate paragraphs.
    So the prompt can be wrapped for reading without changing what the model sees."""
    paragraphs = re.split(r"\n\s*\n", path.read_text(encoding="utf-8").strip())
    return "\n\n".join(" ".join(ln.strip() for ln in p.splitlines() if ln.strip()) for p in paragraphs)


def compile_pattern(name: str, spec: dict) -> re.Pattern:
    if not isinstance(spec, dict) or ("terms" in spec) == ("regex" in spec):
        raise ProfileError(f"rules.yaml: pattern '{name}' needs exactly one of 'terms' or 'regex'")
    if "terms" in spec:
        terms = spec["terms"]
        if not terms or not all(isinstance(t, str) and t for t in terms):
            raise ProfileError(f"rules.yaml: pattern '{name}': 'terms' must be a list of words")
        source = r"\b(" + "|".join(terms) + r")\b"
    else:
        regex = spec["regex"]
        source = "".join(regex) if isinstance(regex, list) else regex
    try:
        return re.compile(source, re.IGNORECASE)
    except re.error as e:
        raise ProfileError(f"rules.yaml: pattern '{name}' is not a valid regex: {e}") from e


def _rules(raw: list, stage: str, patterns: dict[str, re.Pattern]) -> tuple[Rule, ...]:
    if not isinstance(raw, list):
        raise ProfileError(f"rules.yaml: '{stage}' must be a list of rules")
    rules = []
    for i, r in enumerate(raw, 1):
        where = f"rules.yaml: {stage} rule {i}"
        category = _require(r, "category", where)
        all_names, any_names, none_names = r.get("all", []), r.get("any", []), r.get("none", [])
        if not all_names and not any_names:
            raise ProfileError(f"{where}: needs 'all' or 'any'")
        for n in [*all_names, *any_names, *none_names]:
            if n not in patterns:
                raise ProfileError(f"{where}: unknown pattern '{n}'")
        scope = r.get("scope", "text")
        if scope not in SCOPES:
            raise ProfileError(f"{where}: scope must be one of {sorted(SCOPES)}")
        rules.append(Rule(category, r.get("reason", category),
                          tuple(patterns[n] for n in all_names),
                          tuple(patterns[n] for n in any_names), scope,
                          tuple(patterns[n] for n in none_names)))
    return tuple(rules)


def _models(raw, where: str) -> dict[str, dict]:
    if not isinstance(raw, dict) or not raw:
        raise ProfileError(f"{where}: 'models' must name at least one model")
    models = {}
    for key, m in raw.items():
        w = f"{where}: model '{key}'"
        if not isinstance(m, dict):
            raise ProfileError(f"{w}: must be a mapping")
        if "api_key" in m:
            raise ProfileError(f"{w}: never put an API key in a profile; use api_key_env "
                               "with the name of an environment variable")
        provider = _require(m, "provider", w)
        if provider not in PROVIDERS:
            raise ProfileError(f"{w}: provider must be one of {sorted(PROVIDERS)}")
        _require(m, "name", w)
        location = _require(m, "location", w)
        if location not in LOCATIONS:
            raise ProfileError(f"{w}: location must be local or external")
        if provider == "openai_compatible":
            _require(m, "base_url", w)
        if provider in {"ollama", "mock"} and location != "local":
            raise ProfileError(f"{w}: provider {provider} runs locally; location must be local")
        if provider in HOSTED_PROVIDERS:
            # A hosted model marked local would receive unmasked text: never allowed.
            if location != "external":
                raise ProfileError(f"{w}: provider {provider} is a third-party service; "
                                   "location must be external")
            _require(m, "api_key_env", w)
        models[key] = dict(m)
    return models


def _routing(raw, models: dict[str, dict], where: str) -> dict:
    w = f"{where}: routing"
    if not isinstance(raw, dict):
        raise ProfileError(f"{w}: missing")
    r = {
        "local_model": _require(raw, "local_model", w),
        "external_model": raw.get("external_model"),
        "external_allowed": raw.get("external_allowed", False),
        "send_external_when": raw.get("send_external_when", "no_personal_info"),
        "never_external_entities": tuple(raw.get("never_external_entities") or []),
        "never_external_labels": tuple(raw.get("never_external_labels") or []),
        "local_input": raw.get("local_input", "masked"),
        "fallback": tuple(raw.get("fallback") or []),
    }
    lm = models.get(r["local_model"])
    if lm is None or lm["location"] != "local":
        raise ProfileError(f"{w}: local_model '{r['local_model']}' must be a model with location local")
    if r["external_model"] is not None:
        em = models.get(r["external_model"])
        if em is None or em["location"] != "external":
            raise ProfileError(f"{w}: external_model '{r['external_model']}' must be a model "
                               "with location external")
    if not isinstance(r["external_allowed"], bool):
        raise ProfileError(f"{w}: external_allowed must be true or false")
    if r["external_allowed"] and r["external_model"] is None:
        raise ProfileError(f"{w}: external_allowed is true but no external_model is set")
    if r["send_external_when"] not in SEND_EXTERNAL_WHEN:
        raise ProfileError(f"{w}: send_external_when must be one of {sorted(SEND_EXTERNAL_WHEN)}")
    if r["local_input"] not in LOCAL_INPUT:
        raise ProfileError(f"{w}: local_input must be masked or original")
    _check_fallback(r, models, w)
    return r


def _check_fallback(r: dict, models: dict[str, dict], w: str) -> None:
    """fallback: other external models to try, in order, if the external model fails or
    has no key. The local model is always the last resort, so it is never listed."""
    if r["fallback"] and r["external_model"] is None:
        raise ProfileError(f"{w}: fallback needs an external_model")
    seen = {r["external_model"]}
    for key in r["fallback"]:
        m = models.get(key)
        if m is None or m["location"] != "external":
            raise ProfileError(f"{w}: fallback '{key}' must be a model with location external "
                               "(the local model is always the last resort)")
        if key in seen:
            raise ProfileError(f"{w}: fallback lists '{key}' twice or repeats the external model")
        seen.add(key)


def select_external_model(profile: Profile, key: str) -> Profile:
    """The same profile with another of its external models in front (EXTERNAL_MODEL)."""
    w = f"{profile.name}: EXTERNAL_MODEL={key}"
    m = profile.models.get(key)
    if m is None or m["location"] != "external":
        names = sorted(k for k, v in profile.models.items() if v["location"] == "external")
        raise ProfileError(f"{w}: not one of this profile's external models ({', '.join(names) or 'none'})")
    routing = {**profile.routing, "external_model": key,
               "fallback": tuple(k for k in profile.routing["fallback"] if k != key)}
    return replace(profile, routing=routing)


def _roles(raw: dict, granted: set[str], where: str, response_keys: set[str]):
    """roles.yaml: data classes, roles (title, tools, data classes, replies), filtered tools."""
    classes = raw.get("data_classes") or {}
    if not isinstance(classes, dict) or not classes:
        raise ProfileError(f"{where}: 'data_classes' must list at least one class")
    roles = {}
    for key, r in (raw.get("roles") or {}).items():
        w = f"{where}: role '{key}'"
        if not NAME_RE.match(key.replace("_", "-")):
            raise ProfileError(f"{w}: use lowercase letters, digits, '-' or '_'")
        tools = tuple((r or {}).get("tools") or [])
        seen = tuple((r or {}).get("data_classes") or [])
        for t in tools:
            if t not in granted:
                raise ProfileError(f"{w}: tool '{t}' is not granted to any agent")
        for c in seen:
            if c not in classes:
                raise ProfileError(f"{w}: unknown data class '{c}'")
        replies = {k: str(v).strip() for k, v in ((r or {}).get("responses") or {}).items()}
        for k, v in replies.items():
            if k not in response_keys:
                raise ProfileError(f"{w}: reply for '{k}', which is not a category in responses.yaml")
            if not v:
                raise ProfileError(f"{w}: reply for '{k}' is empty")
        roles[key] = Role((r or {}).get("title", key), tools, seen, replies)
    if not roles:
        raise ProfileError(f"{where}: 'roles' must define at least one role")
    filtered = tuple(raw.get("filtered_tools") or [])
    for t in filtered:
        if t not in granted:
            raise ProfileError(f"{where}: filtered_tools: '{t}' is not granted to any agent")
    return roles, dict(classes), filtered


def _actions(raw: dict, where: str, patterns: dict[str, re.Pattern], agents: dict[str, tuple[str, ...]],
             roles: dict[str, Role]) -> dict[str, Action]:
    """actions.yaml: changes the assistant may propose. Every action needs a person's
    approval; the tool that makes the change belongs to one agent only, which runs it
    after approval, so the agent that talks to the model can never make a change itself."""
    if not roles:
        raise ProfileError(f"{where}: actions need roles (who may ask, who may approve)")
    actions = {}
    for name, a in (raw or {}).items():
        w = f"{where}: action '{name}'"
        if not isinstance(a, dict):
            raise ProfileError(f"{w}: must be a mapping")
        intent = _require(a, "intent", w)
        if intent not in patterns:
            raise ProfileError(f"{w}: intent '{intent}' is not a pattern in rules.yaml")
        tool = _require(a, "tool", w)
        agent = a.get("agent", "executor")
        if tool not in agents.get(agent, ()):
            raise ProfileError(f"{w}: tool '{tool}' must be granted to agent '{agent}'")
        others = sorted(n for n, tools in agents.items() if n != agent and tool in tools)
        if others:
            raise ProfileError(f"{w}: only '{agent}' may hold '{tool}'; also granted to {', '.join(others)}")
        fields = a.get("fields") or {}
        if not isinstance(fields, dict) or not fields:
            raise ProfileError(f"{w}: 'fields' must describe at least one field")
        checked = {}
        for f, spec in fields.items():
            spec = dict(spec or {})
            if spec.get("type", "text") not in FIELD_TYPES:
                raise ProfileError(f"{w}: field '{f}': type must be one of {sorted(FIELD_TYPES)}")
            spec.setdefault("type", "text")
            _require(spec, "description", f"{w}: field '{f}'")
            if spec.get("pattern"):
                spec["pattern"] = compile_pattern(f"{name}.{f}", {"regex": spec["pattern"]})
            checked[f] = spec
        propose = tuple(_require(a, "propose_roles", w))
        approve = tuple(_require(a, "approve_roles", w))
        for r in (*propose, *approve):
            if r not in roles:
                raise ProfileError(f"{w}: unknown role '{r}'")
        for r in approve:
            if tool not in roles[r].tools:
                raise ProfileError(f"{w}: approver role '{r}' must have '{tool}' in its tools")
        expires = a.get("expires_minutes", 30)
        if not isinstance(expires, int) or expires <= 0:
            raise ProfileError(f"{w}: expires_minutes must be a positive whole number")
        times = a.get("allowed_times")
        if times is not None:
            f = _require(times, "field", f"{w}: allowed_times")
            if checked.get(f, {}).get("type") != "datetime":
                raise ProfileError(f"{w}: allowed_times.field '{f}' must be a datetime field")
            times = {"field": f, "weekdays": list(times.get("weekdays", range(7))),
                     "start_hour": int(times.get("start_hour", 0)), "end_hour": int(times.get("end_hour", 24))}
        actions[name] = Action(name, a.get("title", name), a.get("description", ""), patterns[intent],
                               tool, agent, checked, propose, approve, expires, times)
    return actions


# --- loading ------------------------------------------------------------------

def load_profile_from(path: Path) -> Profile:
    path = Path(path)
    if not path.is_dir():
        raise ProfileError(f"profile folder not found: {path}")
    p = _yaml(_file(path, "profile.yaml", path.name))
    where = f"{path.name}/profile.yaml"

    name = _require(p, "name", where)
    if not NAME_RE.match(name):
        raise ProfileError(f"{where}: name '{name}' may only use lowercase letters, digits and '-'")
    if name != path.name:
        raise ProfileError(f"{where}: name '{name}' must match the folder name '{path.name}'")

    if "model" in p or "data_policy" in p:
        raise ProfileError(f"{where}: 'model' and 'data_policy' were replaced by 'models' and "
                           "'routing' (see profiles/adhd-assistant/profile.yaml)")
    models = _models(p.get("models"), where)
    routing = _routing(p.get("routing"), models, where)

    responses = _yaml(_file(path, _require(p, "responses", where), where))
    rules_raw = _yaml(_file(path, _require(p, "rules", where), where))
    patterns = {n: compile_pattern(n, s) for n, s in (rules_raw.get("patterns") or {}).items()}
    input_rules = _rules(rules_raw.get("input", []), "input", patterns)
    output_rules = _rules(rules_raw.get("output", []), "output", patterns)

    c = _require(p, "classifier", where)
    cats = tuple(ClassifierCategory(_require(x, "code", f"{where}: classifier category"),
                                    x.get("name", x["code"]),
                                    _require(x, "category", f"{where}: classifier category"))
                 for x in _require(c, "categories", f"{where}: classifier"))
    classifier = ClassifierConfig(
        model=_require(c, "model", f"{where}: classifier"),
        policy=_file(path, _require(c, "policy", f"{where}: classifier"), where).read_text(encoding="utf-8").strip(),
        categories=cats,
        default_category=_require(c, "default_category", f"{where}: classifier"),
    )

    # Narrow overrides of an input classifier flag (e.g. a pure record lookup flagged as
    # medical advice). Never for crisis: a crisis flag can not be overridden.
    exemptions = _rules(rules_raw.get("classifier_exemptions", []), "classifier_exemptions", patterns)
    for i, ex in enumerate(exemptions, 1):
        if ex.category not in {x.category for x in cats} | {classifier.default_category}:
            raise ProfileError(f"rules.yaml: classifier_exemptions rule {i}: '{ex.category}' is not a "
                               "classifier category")
        if ex.category == "crisis":
            raise ProfileError(f"rules.yaml: classifier_exemptions rule {i}: crisis flags can never be overridden")
        if not ex.none:
            raise ProfileError(f"rules.yaml: classifier_exemptions rule {i}: needs 'none' (words that "
                               "cancel the exemption), so it stays narrow")

    source_id_pattern = None
    if p.get("source_id_pattern"):
        try:
            source_id_pattern = re.compile(p["source_id_pattern"])
        except re.error as e:
            raise ProfileError(f"{where}: source_id_pattern is not a valid regex: {e}") from e
        if "ungrounded" not in responses:
            raise ProfileError(f"{path.name}/responses.yaml: no response for ungrounded "
                               "(needed because the profile checks citations)")

    # Every category that can block a message needs a fixed reply.
    used = {r.category for r in input_rules + output_rules}
    used |= {x.category for x in cats} | {classifier.default_category, "unavailable"}
    missing = sorted(used - responses.keys())
    if missing:
        raise ProfileError(f"{path.name}/responses.yaml: no response for {', '.join(missing)}")
    if not all(isinstance(v, str) and v.strip() for v in responses.values()):
        raise ProfileError(f"{path.name}/responses.yaml: every response must be non-empty text")

    agents_raw = _require(p, "agents", where)
    agents = {a: tuple((cfg or {}).get("tools", [])) for a, cfg in agents_raw.items()}
    tool_data_labels = dict(p.get("tool_data_labels") or {})
    granted = {t for tools in agents.values() for t in tools}
    unlabeled = sorted(granted - tool_data_labels.keys())
    if unlabeled:
        raise ProfileError(f"{where}: tool_data_labels has no label for {', '.join(unlabeled)}")

    # Tools that receive real identifiers (e.g. a patient name to look up) must be granted,
    # and the data they return must be labelled as never leaving the machine.
    identifier_tools = tuple(p.get("tools_receiving_identifiers") or [])
    for t in identifier_tools:
        if t not in granted:
            raise ProfileError(f"{where}: tools_receiving_identifiers: '{t}' is not granted to any agent")
        if tool_data_labels.get(t) not in routing["never_external_labels"]:
            raise ProfileError(f"{where}: '{t}' receives identifiers, so its data label "
                               f"'{tool_data_labels.get(t)}' must be in routing.never_external_labels")

    records_db = None
    if p.get("records_db"):
        records_db = (path / p["records_db"]).resolve()
        if path.resolve() not in records_db.parents:
            raise ProfileError(f"{where}: records_db points outside the profile folder")
        # Not required to exist: it is built locally (see the profile's data/README.md).

    roles, data_classes, role_filtered_tools = {}, {}, ()
    if p.get("roles"):
        roles, data_classes, role_filtered_tools = _roles(
            _yaml(_file(path, p["roles"], where)), granted, f"{path.name}/{p['roles']}",
            set(responses))
        if "role_required" not in responses:
            raise ProfileError(f"{path.name}/responses.yaml: no response for role_required "
                               "(needed because the profile has roles)")

    actions = {}
    if p.get("actions"):
        actions = _actions(_yaml(_file(path, p["actions"], where)), f"{path.name}/{p['actions']}",
                           patterns, agents, roles)
        missing = [r for r in ACTION_RESPONSES if r not in responses]
        if missing:
            raise ProfileError(f"{path.name}/responses.yaml: no response for {', '.join(missing)} "
                               "(needed because the profile has actions)")

    privacy = p.get("privacy") or {}
    return Profile(
        name=name,
        title=p.get("title", name),
        description=p.get("description", ""),
        version=int(p.get("version", 1)),
        path=path,
        models=models,
        routing=routing,
        tool_data_labels=tool_data_labels,
        system_prompt=read_prompt(_file(path, _require(p, "prompt", where), where)),
        responses={k: v.strip() for k, v in responses.items()},
        input_rules=input_rules,
        output_rules=output_rules,
        classifier=classifier,
        privacy_entities=tuple(privacy.get("entities", [])),
        privacy_allow_list=tuple(privacy.get("allow_list", [])),
        knowledge_base=_file(path, _require(p, "knowledge_base", where), where),
        records_db=records_db,
        identifier_tools=identifier_tools,
        cite_sources=bool(p.get("cite_sources", False)),
        agents=agents,
        roles=roles,
        data_classes=data_classes,
        role_filtered_tools=role_filtered_tools,
        examples=tuple(p.get("examples") or []),
        classifier_exemptions=exemptions,
        source_id_pattern=source_id_pattern,
        actions=actions,
    )


@lru_cache(maxsize=None)
def load_profile(name: str | None = None) -> Profile:
    """Load a profile by name (default: the PROFILE environment variable).

    For the active profile, EXTERNAL_MODEL=<key> puts another of the profile's external
    models in front, without editing the file (e.g. to compare providers). It can only
    pick a model the profile already lists; anything else stops at startup."""
    active = os.environ.get("PROFILE") or DEFAULT_PROFILE
    name = name or active
    if not NAME_RE.match(name):
        raise ProfileError(f"invalid profile name: {name!r}")
    profile = load_profile_from(PROFILES_DIR / name)
    choice = os.environ.get("EXTERNAL_MODEL")
    if choice and name == active:
        profile = select_external_model(profile, choice)
    return profile


def available_profiles() -> list[str]:
    return sorted(d.name for d in PROFILES_DIR.iterdir() if (d / "profile.yaml").is_file())


if __name__ == "__main__":
    # Check every profile:  uv run python -m profiles.loader
    import sys
    failed = False
    for n in available_profiles():
        try:
            pr = load_profile(n)
            r = pr.routing
            ext = (" -> ".join([r["external_model"], *r["fallback"]]) if r["external_allowed"] else "none")
            print(f"ok   {n}: {len(pr.input_rules)} input rules, {len(pr.output_rules)} output rules, "
                  f"{len(pr.responses)} responses, agents {list(pr.agents)}, "
                  f"local model {pr.local_model['name']}, external model {ext}")
        except ProfileError as e:
            failed = True
            print(f"FAIL {n}: {e}")
    sys.exit(1 if failed else 0)