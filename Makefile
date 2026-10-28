PY = .venv/bin/python

.PHONY: setup test lint demo bench e2e ui

setup:
	python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt

test:
	$(PY) -m pytest -q

demo:
	$(PY) main.py demo

bench:
	$(PY) main.py --bench 20000

e2e:
	$(PY) scripts/e2e_check.py

ui:
	$(PY) main.py serve --demo --db ui_demo.db

lint:
	.venv/bin/ruff check .
	.venv/bin/mypy acme_ap
