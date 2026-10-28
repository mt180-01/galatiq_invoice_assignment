"""Numbers quoted in the documents must be the real ones."""
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def collected() -> int:
    out = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"], cwd=ROOT, capture_output=True, text=True, timeout=120).stdout
    return int(re.search(r"(\d+) tests? collected", out).group(1))


def test_the_test_count_in_the_documents_is_the_real_count_and_the_same_everywhere():
    n = collected()
    quoted = {}
    for name in ("README.md", "CONTRIBUTING.md"):
        quoted[name] = set(re.findall(r"\*{0,2}(\d{3}) tests\*{0,2}", (ROOT / name).read_text()))
    assert quoted == {"README.md": {str(n)}, "CONTRIBUTING.md": {str(n)}}, f"{n} tests exist; documents say {quoted}"


def test_the_readme_names_the_model_the_recordings_were_made_with():
    import json
    readme = (ROOT / "README.md").read_text()
    models = set()
    for path in [*(ROOT / "docs" / "live_runs").glob("*.cassette.jsonl"), *(ROOT / "data" / "extraction_eval").glob("*.cassette.jsonl")]:
        for line in path.read_text().splitlines()[1:]:
            body = json.loads(line).get("body")
            if isinstance(body, dict) and body.get("model"):
                models.add(body["model"])
    assert models and all(m in readme for m in models), models
