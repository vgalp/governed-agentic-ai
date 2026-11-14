"""The training loop's settings, read from modelops/loop.yaml and checked before anything runs (ADR 037).

Every number that decides when the loop stops (targets, rounds, money) is here, validated with bounds,
so a typo cannot turn a $150 run into an unbounded one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT = Path(__file__).resolve().parent / "loop.yaml"


class ConfigError(ValueError):
    """modelops/loop.yaml is missing something or has a value out of bounds."""


@dataclass(frozen=True)
class Budget:
    max_rounds: int
    max_usd: float
    patience: int


@dataclass(frozen=True)
class GuardTargets:
    crisis_recall: float
    false_crisis: float
    decision_leak: float
    dev_macro_f1: float


@dataclass(frozen=True)
class GuardSettings:
    local_judge: str
    train_per_round: int
    gate_per_round: int
    human_audit_rate: float
    reviewer_sample_rate: float
    train_args: dict[str, Any]
    targets: GuardTargets


@dataclass(frozen=True)
class AnswerTargets:
    tool_choice: float
    paraphrase: float
    citations_valid: float
    no_dose: float
    declines_correct: float
    judge_score: float


@dataclass(frozen=True)
class AnswerSettings:
    candidates: tuple[str, ...]
    base_weights: dict[str, str]
    backend: str
    profiles: tuple[str, ...]
    train_per_round: int
    gate_per_round: int
    lora: dict[str, Any]
    judge: bool
    targets: AnswerTargets


@dataclass(frozen=True)
class LoopConfig:
    frontier: dict[str, Any]
    budget: Budget
    guard: GuardSettings
    answer: AnswerSettings
    reviewer: dict[str, Any] | None = None       # a different model for the reviewer; None: the frontier model
    source: Path = field(default=DEFAULT)


def _num(raw: dict, key: str, low: float, high: float, where: str, kind=float):
    if key not in raw:
        raise ConfigError(f"{where}: {key} is required")
    v = raw[key]
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not low <= v <= high:
        raise ConfigError(f"{where}: {key} must be a number from {low:g} to {high:g}")
    if kind is int and not float(v).is_integer():
        raise ConfigError(f"{where}: {key} must be a whole number")
    return kind(v)


def _frontier(raw: dict) -> dict:
    where = "frontier"
    for k in ("provider", "name"):
        if not raw.get(k):
            raise ConfigError(f"{where}: {k} is required")
    if raw["provider"] == "ollama" or raw.get("location", "external") == "local":
        raise ConfigError(f"{where}: must be a hosted frontier model (the local models are the ones being trained "
                          "and the independent second look)")
    price = raw.get("price") or {}
    _num(price, "input_per_million", 0.01, 1000, f"{where}.price")
    _num(price, "output_per_million", 0.01, 1000, f"{where}.price")
    if "parallel" in raw:
        _num(raw, "parallel", 1, 16, where, int)
    if not raw.get("api_key_env"):
        raise ConfigError(f"{where}: api_key_env is required (the environment variable holding the key)")
    for k in ("api_key", "key"):
        if k in raw:
            raise ConfigError(f"{where}: put the key in the environment, not in loop.yaml")
    return {"location": "external", **raw}


def _guard(raw: dict) -> GuardSettings:
    w = "guard"
    t = raw.get("targets") or {}
    targets = GuardTargets(
        crisis_recall=_num(t, "crisis_recall", 0.5, 1.0, f"{w}.targets"),
        false_crisis=_num(t, "false_crisis", 0.0, 0.5, f"{w}.targets"),
        decision_leak=_num(t, "decision_leak", 0.0, 0.5, f"{w}.targets"),
        dev_macro_f1=_num(t, "dev_macro_f1", 0.5, 1.0, f"{w}.targets"))
    if not raw.get("local_judge"):
        raise ConfigError(f"{w}: local_judge is required (the independent second look)")
    return GuardSettings(
        local_judge=str(raw["local_judge"]),
        train_per_round=_num(raw, "train_per_round", 50, 10_000, w, int),
        gate_per_round=_num(raw, "gate_per_round", 30, 2_000, w, int),
        human_audit_rate=_num(raw, "human_audit_rate", 0.0, 1.0, w),
        reviewer_sample_rate=_num(raw, "reviewer_sample_rate", 0.0, 1.0, w),
        train_args=dict(raw.get("train_args") or {}),
        targets=targets)


def _answer(raw: dict) -> AnswerSettings:
    w = "answer"
    t = raw.get("targets") or {}
    targets = AnswerTargets(
        tool_choice=_num(t, "tool_choice", 0.5, 1.0, f"{w}.targets"),
        paraphrase=_num(t, "paraphrase", 0.5, 1.0, f"{w}.targets"),
        citations_valid=_num(t, "citations_valid", 0.5, 1.0, f"{w}.targets"),
        no_dose=_num(t, "no_dose", 0.9, 1.0, f"{w}.targets"),
        declines_correct=_num(t, "declines_correct", 0.5, 1.0, f"{w}.targets"),
        judge_score=_num(t, "judge_score", 1.0, 5.0, f"{w}.targets"))
    candidates = tuple(raw.get("candidates") or ())
    if not candidates:
        raise ConfigError(f"{w}: list at least one candidate model")
    backend = raw.get("backend", "mlx")
    if backend not in ("mlx", "peft"):
        raise ConfigError(f"{w}: backend must be mlx or peft")
    lora = dict(raw.get("lora") or {})
    _num(lora, "rank", 2, 256, f"{w}.lora", int)
    _num(lora, "iters", 10, 100_000, f"{w}.lora", int)
    _num(lora, "lr", 1e-6, 1e-2, f"{w}.lora")
    profiles = tuple(raw.get("profiles") or ())
    if not profiles:
        raise ConfigError(f"{w}: list the profiles the examples are written for")
    return AnswerSettings(
        candidates=candidates, base_weights=dict(raw.get("base_weights") or {}), backend=backend,
        profiles=profiles,
        train_per_round=_num(raw, "train_per_round", 20, 10_000, w, int),
        gate_per_round=_num(raw, "gate_per_round", 20, 2_000, w, int),
        lora=lora, judge=bool(raw.get("judge", True)), targets=targets)


def load(path: Path = DEFAULT) -> LoopConfig:
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        raise ConfigError(f"{path} not found") from None
    b = raw.get("budget") or {}
    budget = Budget(max_rounds=_num(b, "max_rounds", 1, 100, "budget", int),
                    max_usd=_num(b, "max_usd", 1, 10_000, "budget"),
                    patience=_num(b, "patience", 1, 20, "budget", int))
    reviewer = _frontier(raw["reviewer"]) if raw.get("reviewer") else None
    return LoopConfig(_frontier(raw.get("frontier") or {}), budget, _guard(raw.get("guard") or {}),
                      _answer(raw.get("answer") or {}), reviewer, Path(path))
