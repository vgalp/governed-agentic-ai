"""Shared test setup. Tests read the profiles as committed, whatever a developer has exported for a trial."""

import pytest

TRIAL_SETTINGS = ("GOVERNED_TEXT_MODEL", "GOVERNED_TEXT_MODEL_MODE", "GOVERNED_ENV", "GATEWAY_TOKEN", "GATEWAY_TOKENS")


@pytest.fixture(autouse=True)
def no_trial_settings(monkeypatch):
    """A classifier turned on from the environment, a production flag or a saved token must not reach a test."""
    for name in TRIAL_SETTINGS:
        monkeypatch.delenv(name, raising=False)
