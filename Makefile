PY = .venv/bin/python

.PHONY: setup test demo bench

setup:
	python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

test:
	$(PY) -m pytest -q

demo:  ## sample invoices, then the over-limit one with a simulated VP approval
	$(PY) main.py --invoice_path=data/invoices
	$(PY) main.py --invoice_path=data/approvals_demo --auto-vp
	$(PY) pay.py status

bench:
	$(PY) main.py --bench 20000
