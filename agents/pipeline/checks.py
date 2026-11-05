"""Output checks on a model-written answer, in the order they run (OUTPUT_CHECKS).

Each check takes the runtime and the request, records its own trace step, and returns a
GuardrailDecision, or None when it does not apply to this profile or answer. The first check that
blocks ends the request; `check.layer` names the layer in the audit and the UI. Adding a check is
adding a function here and its name to the list.

Toxicity is measured, never a reason to block (ADR 018), so it is not a check.
"""

from agents.pipeline.context import Request
from agents.pipeline.runtime import Runtime
from agents.trace import ids_text
from guardrails import faithfulness, toxicity
from guardrails.classifier import classifier_check
from guardrails.grounding import check_citations
from guardrails.rules import GuardrailDecision, check_output
from llm import usage


def layer(name: str):
    def mark(fn):
        fn.layer = name
        return fn

    return mark


def measure_toxicity(rt: Runtime, r: Request) -> None:
    tox = toxicity.score(r.restored_answer)
    if tox is None:
        return
    meter = usage.current()
    meter.toxicity, meter.toxicity_model = tox, toxicity.MODEL
    r.trace.step(
        "answer",
        "toxicity",
        "info",
        f"Toxicity {tox:.2f} ({'high: shown for review' if tox >= toxicity.FLAG_AT else 'low'}); "
        "measured only, the checks below decide",
        toxicity=tox,
        high=tox >= toxicity.FLAG_AT,
    )


@layer("rules")
def output_rules(rt: Runtime, r: Request) -> GuardrailDecision:
    d = check_output(r.restored_answer, rt.profile)
    r.trace.step(
        "answer",
        "output_rules",
        "pass" if d.allowed else "block",
        "No output rule matched" if d.allowed else f"Blocked by rule: {d.reason}",
    )
    return d


@layer("grounding")
def grounding(rt: Runtime, r: Request) -> GuardrailDecision | None:
    """An answer may cite only sources the model was given (profiles with source IDs)."""
    P = rt.profile
    if P.source_id_pattern is None:
        return None
    d = check_citations(r.restored_answer, [n["id"] for n in r.notes], P)
    cited = sorted({m.group(0) for m in P.source_id_pattern.finditer(r.restored_answer)})
    r.trace.step(
        "answer",
        "grounding",
        "pass" if d.allowed else "block",
        (f"Cites {ids_text(cited)}, all given to the model" if cited else "Cites no sources")
        if d.allowed
        else f"Blocked: {d.reason}",
        cited=cited,
    )
    return d


@layer("classifier")
def output_classifier(rt: Runtime, r: Request) -> GuardrailDecision:
    """The safety classifier reads the answer with the question."""
    r.trace.start("answer", "output_classifier", "Safety classifier checking the answer with the question")
    d = classifier_check(r.message, r.restored_answer, rt.profile)
    g = rt.guards["answer"]
    r.trace.step(
        "answer",
        "output_classifier",
        "pass" if d.allowed else "block",
        f"{g}: safe" if d.allowed else f"Blocked by {g}: {d.category}",
    )
    return d


@layer("faithfulness")
def faithfulness_judge(rt: Runtime, r: Request) -> GuardrailDecision | None:
    """A second model judges an answer written from listed tools' results against them (ADR 022).
    In mode warn, an unconfirmed (not contradicted) claim lets the answer through with a note."""
    cfg = rt.faithfulness
    if cfg is None:
        return None
    r.trace.start("answer", "faithfulness", f"{cfg.judge['name']} judging the answer against its sources")
    d, verdict = faithfulness.check(
        r.restored_answer, r.notes_text, cfg, rt.chat, sorted(set(r.tools_used) | set(r.triggered)), rt.profile
    )
    if not verdict.get("ran"):
        r.trace.step("answer", "faithfulness", "info", "Not needed: no judged tool answered this question")
        return None
    if verdict.get("warning"):
        r.restored_answer += "\n\n" + rt.profile.response("not_confirmed", r.role)
    r.trace.step(
        "answer",
        "faithfulness",
        "pass" if d.allowed else "block",
        (
            f"{verdict['judge']}: its reply could not be read; nothing found contradicted, answer sent with a warning"
            if verdict.get("warning") and verdict.get("unreadable")
            else f"{verdict['judge']}: {verdict['unsupported']} claim(s) not found in the pages, none contradicted; "
            "answer sent with a warning"
            if verdict.get("warning")
            else f"{verdict['judge']}: every claim is in the sources"
        )
        if d.allowed
        else f"Withheld by {verdict['judge']}: {d.reason}",
        **verdict,
    )
    return d


OUTPUT_CHECKS = (output_rules, grounding, output_classifier, faithfulness_judge)
