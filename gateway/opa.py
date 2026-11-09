"""Talk to OPA: load a profile's settings and ask for decisions.

Profile settings (tool permissions, routing) are loaded into OPA under
data.profiles.<name>. They are reloaded whenever they differ from the profile,
for example after OPA restarted.

OPA requires a token when OPA_TOKEN is set (policies/system/authz.rego, audit SC-5, ADR 030). It
is read from the environment, or from the repository's .env, where scripts/dev.sh puts it, so a
process started some other way (the governed endpoint from another folder) still has it.
"""

import os
from functools import lru_cache
from pathlib import Path

import requests

from profiles.loader import Profile

OPA_URL = "http://127.0.0.1:8181"
ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


@lru_cache(maxsize=1)
def opa_token() -> str | None:
    if os.environ.get("OPA_TOKEN"):
        return os.environ["OPA_TOKEN"]
    try:
        for line in ENV_FILE.read_text().splitlines():
            if line.startswith("OPA_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"') or None
    except OSError:
        pass
    return None


def _headers() -> dict:
    token = opa_token()
    return {"Authorization": f"Bearer {token}"} if token else {}


def ensure_profile_data(profile: Profile) -> None:
    url = f"{OPA_URL}/v1/data/profiles/{profile.name}"
    resp = requests.get(url, headers=_headers(), timeout=5)
    resp.raise_for_status()
    if resp.json().get("result") != profile.policy_data():
        requests.put(url, json=profile.policy_data(), headers=_headers(), timeout=5).raise_for_status()


def decide(profile: Profile, path: str, input_: dict):
    """Evaluate data.<path> for this input, e.g. path "mcp/authz/allow"."""
    ensure_profile_data(profile)
    resp = requests.post(f"{OPA_URL}/v1/data/{path}", json={"input": input_}, headers=_headers(), timeout=5)
    resp.raise_for_status()
    return resp.json().get("result")
