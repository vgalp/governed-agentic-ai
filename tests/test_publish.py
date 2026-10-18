"""Publishing and fetching the guard classifier: the card is built from the record, results from other
weights are refused, and a download with the wrong hash is never installed. No torch, no network."""

import json
from pathlib import Path

import pytest

from classifier import fetch as fetch_mod
from classifier import publish
from classifier.publish import PublishError, sha256


@pytest.fixture
def model_dir(tmp_path):
    m = tmp_path / "models" / "guard-test"
    m.mkdir(parents=True)
    (m / "model.safetensors").write_bytes(b"weights-v1")
    (m / "config.json").write_text("{}")
    (m / "tokenizer.json").write_text("{}")
    (m / "tokenizer_config.json").write_text("{}")
    (m / "thresholds.json").write_text(json.dumps({"thresholds": {"crisis": 0.25, "decision": 0.55}}))
    (m / "training_info.json").write_text(json.dumps({
        "base_model": "answerdotai/ModernBERT-base", "trained": "2026-10-12", "device": "mps",
        "best_dev": {"macro_f1": 0.95, "crisis": {"recall": 0.97}}}))
    data = tmp_path / "data"
    data.mkdir()
    rows = [{"profile": "adhd-assistant", "label": "crisis", "text": "x", "generator": "qwen2.5:7b"},
            {"profile": "clinic-assistant", "label": "allow", "text": "y", "generator": "qwen2.5:7b"}]
    (data / "train.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    (data / "dev.jsonl").write_text(json.dumps({**rows[0], "text": "z"}))
    (data / "rejected.jsonl").write_text(json.dumps(rows[1]))
    (data / "reviewed.csv").write_text("id,profile,label,your_label,reviewer,note\ng-1,a,crisis,crisis,me,\n")
    return m


def results_file(tmp_path, weights_sha):
    s = {"n": 150, "crisis_missed": 3, "crisis_total": 30, "decisions_let_through": 6, "decision_total": 48,
         "wrong_blocks": 1, "allow_total": 72, "false_crisis": 0, "accuracy": 0.9333, "macro_f1": 0.9349}
    p = tmp_path / "guard_test_v4_classifier_abc.json"
    p.write_text(json.dumps({"model_sha256": weights_sha, "test_set": "v4", "summary": {"all": s}}))
    return p


def test_the_release_and_card_come_from_the_record(tmp_path, model_dir):
    weights = sha256(model_dir / "model.safetensors")
    out = publish.build("guard-test", [results_file(tmp_path, weights)], models_dir=model_dir.parent,
                        data_dir=tmp_path / "data", release_dir=tmp_path / "release")
    card = (out / "README.md").read_text()
    assert "license: apache-2.0" in card and "base_model: answerdotai/ModernBERT-base" in card
    assert "missed 3 of 30 crisis messages" in card and "| v4, all profiles | 150 | 140 / 150 | 0.935 |" in card
    assert "3 synthetic messages" in card and "qwen2.5:7b (3)" in card and "1 messages\n  were checked" in card
    assert weights in card and "Not a medical device" in card
    manifest = json.loads((out / "MANIFEST.json").read_text())["files"]
    assert manifest["model.safetensors"] == weights and "LICENSE" in manifest and "README.md" in manifest


def test_results_scored_on_other_weights_are_refused(tmp_path, model_dir):
    with pytest.raises(PublishError, match="not on these"):
        publish.build("guard-test", [results_file(tmp_path, "0" * 64)], models_dir=model_dir.parent,
                      data_dir=tmp_path / "data", release_dir=tmp_path / "release")


def test_an_incomplete_model_folder_is_refused(tmp_path, model_dir):
    (model_dir / "thresholds.json").unlink()
    with pytest.raises(PublishError, match="thresholds.json"):
        publish.build("guard-test", [], models_dir=model_dir.parent, data_dir=tmp_path / "data",
                      release_dir=tmp_path / "release")


def registry(tmp_path, sha):
    r = tmp_path / "published.yaml"
    r.write_text(f"guard-test:\n  repo: someone/guard-test\n  revision: abc123\n  sha256: {sha}\n")
    return r


def fake_download(content: bytes, seen: list):
    def download(repo_id, revision, local_dir):
        seen.append((repo_id, revision))
        Path(local_dir).mkdir(parents=True)
        (Path(local_dir) / "model.safetensors").write_bytes(content)
    return download


def test_fetch_installs_only_the_recorded_weights(tmp_path):
    good = b"weights-v1"
    reg, models, seen = registry(tmp_path, publish.hashlib.sha256(good).hexdigest()), tmp_path / "models", []
    models.mkdir()
    path = fetch_mod.fetch("guard-test", reg, models, fake_download(good, seen))
    assert (path / "model.safetensors").read_bytes() == good and seen == [("someone/guard-test", "abc123")]
    fetch_mod.fetch("guard-test", reg, models, fake_download(b"other", seen))       # already right: no download
    assert len(seen) == 1


def test_fetch_refuses_a_different_model(tmp_path):
    reg, models = registry(tmp_path, "1" * 64), tmp_path / "models"
    models.mkdir()
    with pytest.raises(fetch_mod.FetchError, match="does not match"):
        fetch_mod.fetch("guard-test", reg, models, fake_download(b"tampered", []))
    assert not (models / "guard-test").exists() and not list(models.iterdir())


def test_an_unpublished_model_says_what_to_do(tmp_path):
    reg = tmp_path / "published.yaml"
    reg.write_text("guard-test:\n  repo:\n  revision:\n  sha256: abc\n")
    with pytest.raises(fetch_mod.FetchError, match="not published yet"):
        fetch_mod.record("guard-test", reg)
