"""The shape of every profile file, as pydantic models (ADR 026).

The loader (profiles/loader.py) validates each file against its model here before it runs its own
semantic checks (which need several files at once: a tool granted to no agent, a judge that is
also an answering model, a response a rule needs). This module answers the simpler question
first, with a field path in the error: is this file the right shape?

Unknown keys are errors. Before this, a misspelled key (`on_emtpy:`) was silently ignored and the
feature it configured silently off; for a governance layer that is the wrong default.

`scripts/profile_schema.py` writes these models as JSON Schema to docs/schemas/, so an office can
validate a profile in an editor or a CI job without running Python.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

PROVIDERS = Literal["ollama", "openai", "gemini", "anthropic", "openai_compatible", "mock"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)  # strict: "yes" is not a boolean, 1 is not text


# --- profile.yaml -------------------------------------------------------------------------------


class Price(Strict):
    input_per_million: float = Field(ge=0)
    output_per_million: float = Field(ge=0)


class Model(Strict):
    provider: PROVIDERS
    name: str
    location: Literal["local", "external"]
    temperature: float | None = None
    max_tokens: int | None = Field(default=None, ge=1)
    api_key_env: str | None = None  # the NAME of the environment variable; never the key
    base_url: str | None = None
    price: Price | None = None  # US dollars per million tokens (ADR 018)


class Routing(Strict):
    local_model: str
    external_model: str | None = None
    external_allowed: bool = False
    fallback: list[str] = []
    send_external_when: Literal["no_personal_info", "masked"] = "no_personal_info"
    never_external_entities: list[str] = []
    never_external_labels: list[str] = []
    local_input: Literal["masked", "original"] = "masked"


class Category(Strict):
    code: str
    name: str
    category: str


class Classifier(Strict):
    model: str
    policy: str
    categories: list[Category]
    default_category: str
    decision_category: str | None = None
    input_engine: Literal["llamaguard", "guard_model"] = "llamaguard"
    guard_model: str | None = None
    guard_policy: str | None = None
    guard_policy_of: str | None = None  # another profile's guard policy, by name
    input_agent: str | None = None  # a key in models


class Privacy(Strict):
    entities: list[str] = []
    allow_list: list[str] = []
    allow_list_files: list[str] = []  # relative to the repository root, e.g. privacy/drug_names.txt
    local_model_sees: list[str] = []  # entity types restored for the LOCAL model only (never PERSON)


class Faithfulness(Strict):
    judge: str  # a key in models, never an answering model
    for_tools: list[str] = []
    strict: bool = True
    mode: Literal["withhold", "warn"] = "withhold"


class Agent(Strict):
    tools: list[str] = []


class OutsideListen(Strict):
    host: str = "127.0.0.1"
    port: int = Field(default=8110, gt=0, lt=65536)


class OutsideAuth(Strict):
    kind: str
    url: str
    secret_env: str  # the NAME of the environment variable
    cache_seconds: int = Field(default=30, ge=0, le=300)


class Outside(Strict):
    agent: str
    auth: OutsideAuth
    listen: OutsideListen = OutsideListen()
    mask_results: Literal["external", "always", "never"] = "external"


class ProfileFile(Strict):
    """profile.yaml"""

    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    title: str | None = None
    description: str | None = None
    version: int = 1
    models: dict[str, Model]
    routing: Routing
    tools: str  # file names, relative to the profile folder
    prompt: str
    responses: str
    rules: str
    classifier: Classifier
    privacy: Privacy = Privacy()
    knowledge_base: str
    records_db: str | None = None
    records_map: str | None = None
    fhir: str | None = None
    roles: str | None = None
    actions: str | None = None
    outside: str | None = None  # outside.yaml (ADR 015), validated with Outside
    agents: dict[str, Agent]
    cite_sources: bool = False
    source_id_pattern: str | None = None
    faithfulness: Faithfulness | None = None
    examples: list[str] = []


# --- tools.yaml ---------------------------------------------------------------------------------


class ToolSpec(Strict):
    description: str = ""
    server: str
    kind: Literal["read", "change"]
    data_label: str
    lane: Literal["knowledge", "records"] | None = None  # read tools
    receives_identifiers: bool = False
    role_filtered: bool = False
    tenant_scoped: bool = False
    when: str | None = None  # a pattern name in rules.yaml
    answer: Literal["model", "direct"] = "model"
    keeps: list[str] = []  # masked entity types restored in the query (never PERSON)
    on_empty: str | None = None  # a reply in responses.yaml
    timeout: float | None = Field(default=None, gt=0, le=600)  # seconds; default 30 (gateway)


class ToolsFile(Strict):
    """tools.yaml: tool name -> spec"""

    model_config = ConfigDict(extra="allow")

    @classmethod
    def check(cls, raw: Any, where: str) -> None:
        if not isinstance(raw, dict) or not raw:
            raise SchemaError(f"{where}: must be a map of tool name -> spec")
        for name, spec in raw.items():
            validate(ToolSpec, spec, f"{where}: {name}")


# --- roles.yaml ---------------------------------------------------------------------------------


class RoleSpec(Strict):
    title: str
    tools: list[str] = []
    data_classes: list[str] = []
    responses: dict[str, str] = {}


class RolesFile(Strict):
    """roles.yaml"""

    data_classes: dict[str, str]
    roles: dict[str, RoleSpec]


# --- rules.yaml ---------------------------------------------------------------------------------


class Pattern(Strict):
    terms: list[str] | None = None
    regex: str | list[str] | None = None


class Rule(Strict):
    category: str
    reason: str | None = None
    any: list[str] = []
    all: list[str] = []
    none: list[str] = []
    scope: Literal["text", "sentence"] | None = None


class RulesFile(Strict):
    """rules.yaml"""

    patterns: dict[str, Pattern]
    input: list[Rule] = []
    output: list[Rule] = []
    classifier_exemptions: list[Rule] = []


# --- actions.yaml -------------------------------------------------------------------------------


class ActionField(Strict):
    type: Literal["source", "datetime", "text"]
    description: str
    pattern: str | None = None


class AllowedTimes(Strict):
    field: str
    weekdays: list[int] | None = None
    start_hour: int | None = Field(default=None, ge=0, le=24)
    end_hour: int | None = Field(default=None, ge=0, le=24)


class Change(Strict):
    record: str
    id_field: str
    set_from: dict[str, str] = {}
    set: dict[str, Any] = {}
    only_if: dict[str, Any] = {}


class ActionSpec(Strict):
    title: str | None = None
    description: str | None = None
    intent: str
    tool: str
    agent: str = "executor"
    fields: dict[str, ActionField] = {}
    propose_roles: list[str]
    approve_roles: list[str]
    expires_minutes: int = Field(default=30, ge=1)
    allowed_times: AllowedTimes | None = None
    change: Change | None = None
    done_text: str | None = None


class ActionsFile(Strict):
    """actions.yaml: action name -> spec"""

    model_config = ConfigDict(extra="allow")

    @classmethod
    def check(cls, raw: Any, where: str) -> None:
        if not isinstance(raw, dict):
            raise SchemaError(f"{where}: must be a map of action name -> spec")
        for name, spec in raw.items():
            validate(ActionSpec, spec, f"{where}: {name}")


# --- responses.yaml -----------------------------------------------------------------------------


class ResponsesFile(Strict):
    """responses.yaml: category -> fixed reply"""

    model_config = ConfigDict(extra="allow")

    @classmethod
    def check(cls, raw: Any, where: str) -> None:
        if not isinstance(raw, dict) or not raw:
            raise SchemaError(f"{where}: must be a map of category -> reply text")
        bad = [k for k, v in raw.items() if not isinstance(v, str)]
        if bad:
            raise SchemaError(f"{where}: {', '.join(bad)}: a reply must be text")


# --- running a check ----------------------------------------------------------------------------


class SchemaError(ValueError):
    """A profile file is not the shape its schema describes (field path and reason in the message)."""


# Unknown keys that deserve a better message than "misspelled?".
HINTS = {
    "api_key": "never put an API key in a profile; api_key_env names the environment variable that holds it",
    "model": "replaced by 'models' (a map of model keys) and routing.local_model",
    "tool_data_labels": "moved to tools.yaml (data_label on each tool)",
    "tools_receiving_identifiers": "moved to tools.yaml (receives_identifiers on each tool)",
}


def _describe(e: ValidationError) -> str:
    parts = []
    for err in e.errors():
        path = ".".join(str(p) for p in err["loc"]) or "(top level)"
        msg = err["msg"]
        if err["type"] == "extra_forbidden":
            msg = HINTS.get(str(err["loc"][-1]), "unknown key (misspelled?)")
        parts.append(f"{path}: {msg}")
    return "; ".join(parts)


def validate(model: type[BaseModel], raw: Any, where: str) -> BaseModel:
    """The parsed model, or SchemaError naming the file, the field and the problem."""
    try:
        return model.model_validate(raw)
    except ValidationError as e:
        raise SchemaError(f"{where}: {_describe(e)}") from None


FILES = {  # file -> model, for the JSON Schema export (scripts/profile_schema.py)
    "profile": ProfileFile,
    "tools": ToolSpec,  # tools.yaml is a map of tool name -> this
    "roles": RolesFile,
    "rules": RulesFile,
    "actions": ActionSpec,  # actions.yaml is a map of action name -> this
    "outside": Outside,
}
