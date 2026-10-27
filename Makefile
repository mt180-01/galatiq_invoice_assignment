PY = .venv/bin/python

.PHONY: setup test demo bench

setup:
	python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

test:
	$(PY) -m pytest -q

demo:
	$(PY) main.py demo

bench:
	$(PY) main.py --bench 20000
