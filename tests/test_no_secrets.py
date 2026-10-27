"""A public repo must never hold a credential. These fail loudly if one is ever staged or committed."""
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TOKEN = re.compile(r"xai-[A-Za-z0-9_\-]{16,}|grok_[0-9a-f]{16,}")   # key shapes; a real key must never appear in this file
GENERIC = re.compile(r"(?i)(api[_-]?key|secret|token)\s*[=:]\s*['\"][A-Za-z0-9_\-]{24,}['\"]")


def tracked_files():
    try:
        out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout.decode()
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    return [ROOT / f for f in out.split("\0") if f]


def test_no_api_key_in_any_tracked_file():
    hits = []
    for f in tracked_files():
        try:
            text = f.read_text(errors="ignore")
        except OSError:
            continue
        found = [m.group(0) for m in TOKEN.finditer(text)] + [m.group(0) for m in GENERIC.finditer(text)]
        if [x for x in found if "not-a-real-key" not in x]:      # test fixtures are labelled as fake; anything else is a leak
            hits.append(str(f.relative_to(ROOT)))
    assert not hits, f"possible credential in: {hits}"


def test_credential_files_are_git_ignored():
    ignored = (ROOT / ".gitignore").read_text().split()
    assert {".env", "xai_key", "*.key", "*.llmcache"} <= set(ignored)


def test_the_key_is_read_from_outside_the_project(tmp_path, monkeypatch):
    from acme_ap.llm import read_api_key
    key_file = tmp_path / "k"
    key_file.write_text("xai-example-not-a-real-key-0000\n")
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    monkeypatch.setenv("XAI_API_KEY_FILE", str(key_file))
    assert read_api_key() == "xai-example-not-a-real-key-0000"
    monkeypatch.setenv("XAI_API_KEY", "from-env")
    assert read_api_key() == "from-env"                                   # the environment wins
    monkeypatch.delenv("XAI_API_KEY")
    monkeypatch.setenv("XAI_API_KEY_FILE", str(tmp_path / "missing"))
    assert read_api_key() is None


def test_the_key_never_appears_in_error_text(monkeypatch):
    """A failed call must not echo the Authorization header back into logs."""
    import asyncio
    import httpx
    from acme_ap.llm import GrokClient, LLMUnavailable
    secret = "xai-example-not-a-real-key-1111"
    llm = GrokClient(secret, transport=httpx.MockTransport(lambda r: httpx.Response(401, text="invalid key")))
    with pytest.raises(LLMUnavailable) as err:
        asyncio.run(llm.chat_json("s", "u"))
    assert secret not in str(err.value)


def test_a_rejected_key_stops_further_calls_and_shows_the_servers_reason_without_the_key():
    import asyncio
    import httpx
    from acme_ap.llm import GrokClient, LLMUnavailable
    secret, calls = "xai-example-not-a-real-key-2222", []

    def handler(request):
        calls.append(1)
        return httpx.Response(400, json={"code": "invalid-argument", "error": f"Incorrect API key provided: {secret}"})
    llm = GrokClient(secret, transport=httpx.MockTransport(handler))
    assert llm.available
    with pytest.raises(LLMUnavailable) as first:
        asyncio.run(llm.chat_json("s", "u"))
    assert "rejected the API key" in str(first.value) and secret not in str(first.value) and not llm.available
    with pytest.raises(LLMUnavailable, match="disabled"):
        asyncio.run(llm.chat_json("s", "u"))
    assert len(calls) == 1                                                 # the second attempt never touched the network


def test_other_http_errors_show_their_reason():
    import asyncio
    import httpx
    from acme_ap.llm import GrokClient, LLMUnavailable
    llm = GrokClient("xai-example-not-a-real-key-3333", transport=httpx.MockTransport(lambda r: httpx.Response(429, text="rate limit exceeded")))
    with pytest.raises(LLMUnavailable, match="HTTP 429: rate limit exceeded"):
        asyncio.run(llm.chat_json("s", "u"))
    assert llm.available                                                   # a rate limit is temporary: keep the model
