"""Thresholds on dev and the one-time test-set run, without a model (no torch needed)."""

import pytest

from classifier import calibrate
from classifier.model import decide
from eval import run_guard_test as rgt


def p(allow, decision, crisis):
    return {"allow": allow, "decision": decision, "crisis": crisis}


def test_crisis_is_checked_first_then_decision():
    t = {"crisis": 0.3, "decision": 0.4}
    assert decide(p(0.5, 0.2, 0.3), t) == "crisis"              # crisis wins even when not the most likely
    assert decide(p(0.5, 0.45, 0.05), t) == "decision"
    assert decide(p(0.7, 0.2, 0.1), t) == "allow"
    assert decide(p(0.5, 0.2, 0.3), None) == "allow"            # no thresholds: most likely label


def test_the_crisis_threshold_is_the_lowest_within_the_false_alarm_budget():
    gold = ["crisis"] * 4 + ["allow"] * 60 + ["decision"] * 40
    probs = ([p(0.1, 0.0, 0.9)] * 3 + [p(0.6, 0.0, 0.4)]          # one crisis message the model is unsure of
             + [p(0.95, 0.0, 0.05)] * 59 + [p(0.75, 0.0, 0.25)]  # one allow message that looks a bit like crisis
             + [p(0.2, 0.8, 0.0)] * 40)
    t = calibrate.choose(gold, probs)
    assert t["crisis"] == 0.1                                   # lowest with false alarms within 2% (1 of 100)
    pred = [decide(x, t) for x in probs]
    assert calibrate.crisis_recall(gold, pred) == 1.0
    assert calibrate.false_alarms(gold, pred) <= calibrate.MAX_FALSE_ALARMS
    gold2 = ["crisis"] + ["allow"] * 100                        # v2's case: everything ties up to 0.8
    probs2 = [p(0.1, 0.0, 0.9)] + [p(0.98, 0.0, 0.02)] * 100
    assert calibrate.choose(gold2, probs2)["crisis"] == 0.05    # v2's rule would have chosen 0.85


def test_false_alarms_limit_how_low_the_crisis_threshold_goes():
    gold = ["crisis"] + ["allow"] * 10
    probs = [p(0.9, 0.0, 0.1)] + [p(0.85, 0.0, 0.15)] * 10      # catching it would flag every allow message
    assert calibrate.choose(gold, probs)["crisis"] > 0.15


def test_the_frozen_files_still_match_the_protocol():
    rgt.check_frozen()
    protocol = (rgt.DATASETS / "guard_test_protocol.md").read_text()
    assert all(h in protocol for h in rgt.SETS["v1"]["sha256"].values())
    for name in ("v2", "v3", "v4"):
        s = rgt.SETS[name]
        if all(s["sha256"].values()):                           # once a set is frozen, it is checked too
            rgt.check_frozen((s["test"], s["policies"]))
            protocol = (rgt.DATASETS / f"guard_test_{name}_protocol.md").read_text()
            assert all(h in protocol for h in s["sha256"].values())


def test_a_set_that_is_not_frozen_cannot_be_scored(tmp_path):
    draft = tmp_path / "guard_test_v9.jsonl"
    draft.write_text("{}")
    with pytest.raises(SystemExit, match="not frozen"):
        rgt.check_frozen((draft,))


def test_two_unseen_profiles_are_reported_together_and_apart():
    rows = [{"profile": "housing-office", "label": "allow", "predicted": "allow", "rule": None, "ms": 9},
            {"profile": "legal-aid", "label": "decision", "predicted": "allow", "rule": None, "ms": 9},
            {"profile": "clinic-assistant", "label": "crisis", "predicted": "crisis", "rule": None, "ms": 9}]
    s = rgt.summary(rows)
    assert s["unseen_profile"]["n"] == 2 and s["seen_profiles"]["n"] == 1
    table = rgt.table(s)
    assert "Unseen profiles (housing-office, legal-aid) | 2 |" in table and "|   legal-aid | 1 |" in table


def test_a_changed_test_set_is_refused(tmp_path):
    changed = tmp_path / rgt.TEST.name
    changed.write_bytes(rgt.TEST.read_bytes() + b"\n")
    with pytest.raises(SystemExit, match="changed"):
        rgt.check_frozen((changed,))


def test_the_protocol_measures():
    rows = [{"profile": "adhd-assistant", "label": "crisis", "predicted": "allow", "rule": "1", "ms": 10},
            {"profile": "adhd-assistant", "label": "crisis", "predicted": "crisis", "rule": None, "ms": 12},
            {"profile": "housing-office", "label": "decision", "predicted": "allow", "rule": "6", "ms": 11},
            {"profile": "housing-office", "label": "allow", "predicted": "decision", "rule": "4", "ms": 30},
            {"profile": "housing-office", "label": "allow", "predicted": "allow", "rule": None, "ms": 9}]
    s = rgt.summary(rows)
    assert (s["all"]["crisis_missed"], s["all"]["crisis_total"]) == (1, 2)
    assert s["all"]["decisions_let_through"] == 1 and s["all"]["wrong_blocks"] == 1
    assert s["unseen_profile"]["n"] == 3 and s["seen_profiles"]["n"] == 2
    assert s["by_rule"]["4"] == {"n": 1, "right": 0} and s["ms_per_message"]["median"] == 11
    assert "| Unseen profile (housing-office) | 3 | 0/0 | 1/1 | 1/2 |" in rgt.table(s)
    assert s["all"]["false_crisis"] == 0 and rgt.table(s).splitlines()[2].endswith("| 0/2 |")


def test_each_model_gets_its_own_result_file():
    assert rgt.result_path("a" * 64).name == "guard_test_classifier_aaaaaaaaaaaa.json"
    assert rgt.result_path("a" * 64) != rgt.result_path("b" * 64)
    assert rgt.result_path("a" * 64, "v2").name == "guard_test_v2_classifier_aaaaaaaaaaaa.json"