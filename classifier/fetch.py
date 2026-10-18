"""Download a published guard classifier into models/<name>, at a pinned commit, and check its hash.

    uv run --group train python -m classifier.fetch                       # the model the active profile uses
    uv run --group train python -m classifier.fetch guard-modernbert-v4

Which repository, commit and weights hash belong to each model name is recorded in
classifier/published.yaml. The download is refused unless the SHA-256 of model.safetensors matches
that record, so a changed or swapped model is never loaded. Nothing here runs while the pipeline
answers requests: fetching is a setup step you run once.
"""

import argparse
import shutil
import sys
from pathlib import Path

import yaml

from classifier.publish import sha256

ROOT = Path(__file__).resolve().parent.parent
MODELS = ROOT / "models"
REGISTRY = ROOT / "classifier" / "published.yaml"


class FetchError(Exception):
    pass


def record(name: str, registry: Path = REGISTRY) -> dict:
    entries = (yaml.safe_load(registry.read_text()) or {}) if registry.exists() else {}
    r = entries.get(name)
    if not isinstance(r, dict) or not all(r.get(k) not in (None, "") for k in ("repo", "revision", "sha256")):
        raise FetchError(f"{name} is not published yet (no repo, revision and sha256 in {registry.name}). "
                         "Train it (classifier/train.py) or set the profile's input_engine to llamaguard.")
    # YAML reads an all-digit or "1e5"-like hash as a number: always compare as text.
    return {k: str(r[k]).strip() for k in ("repo", "revision", "sha256")}


def fetch(name: str, registry: Path = REGISTRY, models_dir: Path = MODELS, download=None) -> Path:
    """models/<name>, downloaded and verified. An existing folder with the right hash is kept."""
    r = record(name, registry)
    target = models_dir / name
    weights = target / "model.safetensors"
    if weights.exists() and sha256(weights) == r["sha256"]:
        return target
    if download is None:
        from huggingface_hub import snapshot_download     # comes with transformers (train group)
        download = snapshot_download
    tmp = models_dir / f".{name}.download"
    if tmp.exists():
        shutil.rmtree(tmp)
    download(repo_id=r["repo"], revision=r["revision"], local_dir=str(tmp))
    got = sha256(tmp / "model.safetensors") if (tmp / "model.safetensors").exists() else None
    if got != r["sha256"]:
        shutil.rmtree(tmp, ignore_errors=True)
        raise FetchError(f"{r['repo']}@{r['revision'][:12]}: weights hash {str(got)[:12]} does not match the "
                         f"record ({r['sha256'][:12]}). Nothing was installed.")
    if target.exists():
        shutil.rmtree(target)
    tmp.rename(target)
    return target


def main() -> None:
    ap = argparse.ArgumentParser(description="Download a published guard classifier.")
    ap.add_argument("name", nargs="?", help="model name; default: the one the active profile uses")
    args = ap.parse_args()
    name = args.name
    if not name:
        from profiles.loader import load_profile
        p = load_profile()
        if p.classifier.input_engine != "guard_model":
            print(f"Profile {p.name} does not use a guard model (input_engine {p.classifier.input_engine}).")
            return
        name = p.classifier.guard_model
    try:
        path = fetch(name)
    except FetchError as e:
        sys.exit(str(e))
    print(f"{name} ready in {path} (hash verified)")


if __name__ == "__main__":
    main()
