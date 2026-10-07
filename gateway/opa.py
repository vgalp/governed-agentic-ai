"""Talk to OPA: load a profile's settings and ask for decisions.

Profile settings (tool permissions, routing) are loaded into OPA under
data.profiles.<name>. They are reloaded whenever they differ from the profile,
for example after OPA restarted.
"""

import requests

from profiles.loader import Profile

OPA_URL = "http://localhost:8181"


def ensure_profile_data(profile: Profile) -> None:
    url = f"{OPA_URL}/v1/data/profiles/{profile.name}"
    resp = requests.get(url, timeout=5)
    resp.raise_for_status()
    if resp.json().get("result") != profile.policy_data():
        requests.put(url, json=profile.policy_data(), timeout=5).raise_for_status()


def decide(profile: Profile, path: str, input_: dict):
    """Evaluate data.<path> for this input, e.g. path "mcp/authz/allow"."""
    ensure_profile_data(profile)
    resp = requests.post(f"{OPA_URL}/v1/data/{path}", json={"input": input_}, timeout=5)
    resp.raise_for_status()
    return resp.json().get("result")
