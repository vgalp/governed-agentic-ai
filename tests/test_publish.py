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


def benchmark_file(tmp_path, config, system="classifier"):
    def s(k, n):
        return {"k": k, "n": n, "rate": k / n, "ci95": [0.0, 1.0]}
    part = {"crisis_caught": s(9, 10), "false_crisis": s(2, 20), "any_block": s(3, 20)}
    rows = [{"id": "sst_1", "source": "sst", "expected": "crisis", "policy": "a", "predicted": "crisis"},
            {"id": "x_1", "source": "xstest", "expected": "not_crisis", "policy": "a", "predicted": "crisis"},
            {"id": "x_2", "source": "xstest", "expected": "not_crisis", "policy": "a", "predicted": "allow"}]
    summary = {"policies": ["a", "b"], "all": part, "seen_policies": part, "unseen_policies": part,
               "across_policies": {"crisis_missed_under_every_policy": s(1, 5),
                                   "false_crisis_under_any_policy": s(3, 10)}}
    p = tmp_path / "crisis_benchmark_v1_classifier_abc.json"
    p.write_text(json.dumps({"system": system, "config": config, "benchmark_sha256": "f" * 64,
                             "run_at": "2026-10-29T22:50:38", "summary": summary, "rows": rows}))
    return p


def test_the_card_reports_the_crisis_benchmark_and_whether_it_shaped_training(tmp_path, model_dir):
    weights = sha256(model_dir / "model.safetensors")
    args = dict(models_dir=model_dir.parent, data_dir=tmp_path / "data", release_dir=tmp_path / "release",
                benchmark_paths=[benchmark_file(tmp_path, weights[:12])])
    seen = (publish.build("guard-test", [results_file(tmp_path, weights)], benchmark_seen=True, **args)
            / "README.md").read_text()
    assert "### Crisis benchmark v1" in seen and "| All 2 policies | 9 / 10 (90.0%" in seen
    assert "| XSTest (safe prompts that sound unsafe) | – | 1 / 2 (50.0%" in seen
    assert "caught 90.0% of crisis messages and gave the crisis reply to 10.0% of the others (after seeing" in seen
    assert "**Not independent of this release.**" in seen and "False alarms on dark or off-topic" in seen
    unseen = (publish.build("guard-test", [results_file(tmp_path, weights)], **args) / "README.md").read_text()
    assert "Not independent" not in unseen and "Nothing in benchmark v1 was used to train or tune" in unseen


def test_benchmark_results_of_other_weights_or_systems_are_refused(tmp_path, model_dir):
    weights = sha256(model_dir / "model.safetensors")
    for bad in (benchmark_file(tmp_path, "0" * 12), benchmark_file(tmp_path, weights[:12], system="llamaguard")):
        with pytest.raises(PublishError, match="not of these weights"):
            publish.build("guard-test", [results_file(tmp_path, weights)], models_dir=model_dir.parent,
                          data_dir=tmp_path / "data", release_dir=tmp_path / "release", benchmark_paths=[bad])


def test_without_a_benchmark_the_card_is_as_before(tmp_path, model_dir):
    weights = sha256(model_dir / "model.safetensors")
    card = (publish.build("guard-test", [results_file(tmp_path, weights)], models_dir=model_dir.parent,
                          data_dir=tmp_path / "data", release_dir=tmp_path / "release") / "README.md").read_text()
    assert "Crisis benchmark" not in card and "False alarms on dark" not in card


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


def test_a_v2_card_quotes_the_human_labelled_primary_result(tmp_path, model_dir):
    weights = sha256(model_dir / "model.safetensors")
    p = benchmark_file(tmp_path, weights[:12])
    d = json.loads(p.read_text())

    def s(k, n):
        return {"k": k, "n": n, "rate": k / n, "ci95": [0.0, 1.0]}
    primary = {**d["summary"], "all": {"crisis_caught": s(8, 10), "false_crisis": s(1, 20), "any_block": s(1, 20)}}
    d["summary"] = {**d["summary"], "primary_sources": ["openai", "wildguard"], "primary": primary}
    v2 = tmp_path / "crisis_benchmark_v2_classifier_abc.json"
    v2.write_text(json.dumps(d))
    card = (publish.build("guard-test", [results_file(tmp_path, weights)], models_dir=model_dir.parent,
                          data_dir=tmp_path / "data", release_dir=tmp_path / "release",
                          benchmark_paths=[v2]) / "README.md").read_text()
    assert "### Crisis benchmark v2" in card and "WildGuardTest (human labels; the primary result)" in card
    assert "caught 80.0% of crisis messages and gave the crisis reply to 5.0% of the others." in card
    assert "Nothing in benchmark v2 was used to train or tune" in card
