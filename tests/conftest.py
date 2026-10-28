import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


import pytest


@pytest.fixture(autouse=True)
def _never_use_a_real_key(monkeypatch):
    """Tests must be free, offline and repeatable: hide any real key from them, whether it is in the environment or in the key file."""
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.setenv("XAI_API_KEY_FILE", "/nonexistent/xai_key")


@pytest.fixture(autouse=True)
def _instant_retries(monkeypatch):
    """Retry logic is tested, but nobody should wait for it."""
    from acme_ap.llm import GrokClient
    monkeypatch.setattr(GrokClient, "DEFAULT_RETRY", (0, 0))
