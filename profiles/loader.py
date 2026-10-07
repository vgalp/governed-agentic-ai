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
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

PROFILES_DIR = Path(__file__).resolve().parent
DEFAULT_PROFILE = "adhd-assistant"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
SCOPES = {"text", "sentence"}


class ProfileError(Exception):
    """The profile is missing something or contradicts itself."""


@dataclass(frozen=True)
class Rule:
    category: str
    reason: str
    all: tuple[re.Pattern, ...]
    any: tuple[re.Pattern, ...]
    scope: str = "text"


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
class Profile:
    name: str
    title: str
    description: str
    version: int
    path: Path
    model: dict
    data_policy: dict
    system_prompt: str
    responses: dict[str, str]
    input_rules: tuple[Rule, ...]
    output_rules: tuple[Rule, ...]
    classifier: ClassifierConfig
    privacy_entities: tuple[str, ...]
    privacy_allow_list: tuple[str, ...]
    knowledge_base: Path
    agents: dict[str, tuple[str, ...]]          # agent -> tools it may call

    def response(self, category: str) -> str:
        return self.responses[category]

    def policy_data(self) -> dict:
        """What OPA needs to decide tool access for this profile."""
        return {"agents": {a: {"tools": list(t)} for a, t in self.agents.items()}}


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
        all_names, any_names = r.get("all", []), r.get("any", [])
        if not all_names and not any_names:
            raise ProfileError(f"{where}: needs 'all' or 'any'")
        for n in [*all_names, *any_names]:
            if n not in patterns:
                raise ProfileError(f"{where}: unknown pattern '{n}'")
        scope = r.get("scope", "text")
        if scope not in SCOPES:
            raise ProfileError(f"{where}: scope must be one of {sorted(SCOPES)}")
        rules.append(Rule(category, r.get("reason", category),
                          tuple(patterns[n] for n in all_names),
                          tuple(patterns[n] for n in any_names), scope))
    return tuple(rules)


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

    model = _require(p, "model", where)
    _require(model, "provider", f"{where}: model")
    _require(model, "name", f"{where}: model")

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

    privacy = p.get("privacy") or {}
    return Profile(
        name=name,
        title=p.get("title", name),
        description=p.get("description", ""),
        version=int(p.get("version", 1)),
        path=path,
        model=dict(model),
        data_policy=dict(p.get("data_policy") or {"external_models_allowed": False}),
        system_prompt=read_prompt(_file(path, _require(p, "prompt", where), where)),
        responses={k: v.strip() for k, v in responses.items()},
        input_rules=input_rules,
        output_rules=output_rules,
        classifier=classifier,
        privacy_entities=tuple(privacy.get("entities", [])),
        privacy_allow_list=tuple(privacy.get("allow_list", [])),
        knowledge_base=_file(path, _require(p, "knowledge_base", where), where),
        agents=agents,
    )


@lru_cache(maxsize=None)
def load_profile(name: str | None = None) -> Profile:
    """Load a profile by name (default: the PROFILE environment variable)."""
    name = name or os.environ.get("PROFILE") or DEFAULT_PROFILE
    if not NAME_RE.match(name):
        raise ProfileError(f"invalid profile name: {name!r}")
    return load_profile_from(PROFILES_DIR / name)


def available_profiles() -> list[str]:
    return sorted(d.name for d in PROFILES_DIR.iterdir() if (d / "profile.yaml").is_file())


if __name__ == "__main__":
    # Check every profile:  uv run python -m profiles.loader
    import sys
    failed = False
    for n in available_profiles():
        try:
            pr = load_profile(n)
            print(f"ok   {n}: {len(pr.input_rules)} input rules, {len(pr.output_rules)} output rules, "
                  f"{len(pr.responses)} responses, agents {list(pr.agents)}")
        except ProfileError as e:
            failed = True
            print(f"FAIL {n}: {e}")
    sys.exit(1 if failed else 0)
