"""The guard classifier's training script, without training anything (no torch needed)."""

from pathlib import Path

from classifier import train
from classifier.generate import POLICIES


def test_examples_pair_the_profile_policy_with_the_message():
    rows = train.examples(train.TRAIN)
    assert len(rows) > 2000
    assert {policy for policy, _, _ in rows} == set(POLICIES.values())
    assert {label for _, _, label in rows} == {0, 1, 2}


def test_the_training_script_never_reads_the_test_set_or_the_unseen_profile():
    source = Path(train.__file__).read_text()
    assert "guard_test" not in source and "TEST_SET" not in source and "housing-office" not in source
    assert "housing-office" not in POLICIES


def test_metrics_on_a_known_case():
    gold = [0, 0, 1, 1, 2, 2]
    pred = [0, 1, 1, 1, 2, 0]                      # one allow called decision, one crisis missed
    m = train.metrics(gold, pred)
    assert m["accuracy"] == round(4 / 6, 4)
    assert m["crisis"] == {"precision": 1.0, "recall": 0.5, "f1": round(2 / 3, 4)}
    assert m["decision"]["precision"] == round(2 / 3, 4) and m["decision"]["recall"] == 1.0
    assert m["confusion"]["crisis->allow"] == 1 and m["confusion"]["allow->decision"] == 1


def test_the_best_epoch_is_chosen_by_macro_f1_then_crisis_recall():
    a = {"macro_f1": 0.90, "crisis": {"recall": 0.95}}
    b = {"macro_f1": 0.90, "crisis": {"recall": 0.97}}
    c = {"macro_f1": 0.89, "crisis": {"recall": 1.00}}
    assert train.better(a, None) and train.better(b, a) and not train.better(c, b)


def test_the_model_is_saved_outside_git():
    gitignore = (Path(train.__file__).resolve().parent.parent / ".gitignore").read_text().splitlines()
    assert "models/" in gitignore and train.OUT.parent.name == "models"

def test_v2_trains_into_its_own_folder_and_keeps_v1():
    source = Path(train.__file__).read_text()
    assert train.OUT.name == "guard-modernbert"                       # v1 stays where it was scored
    assert 'default="guard-modernbert-v3"' in source and "label_smoothing=args.label_smoothing" in source