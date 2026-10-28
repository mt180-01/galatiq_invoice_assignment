# Acme AP automation

Reads vendor invoices (PDF, scan, email, JSON, XML, CSV, text), checks them against the ERP, routes approvals to the
right person, and pays on the due date through a bank adapter. Built for the case in [docs/ASSIGNMENT.md](docs/ASSIGNMENT.md).

## Quick start (or just `make setup && make demo`)

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt     # 1. set up
.venv/bin/python main.py --invoice_path=data/invoices                  # 2. process the sample invoices (works offline)
.venv/bin/python -m pytest                                             # 3. run the tests
```

`make setup`, `make test`, `make demo` and `make bench` do the same; `make e2e` runs the whole workflow through the command-line tools and checks every step.
A single file works too (`--invoice_path=data/invoices/invoice_1001.txt`). Add `-v` for per-stage logs and `--json out.json`
to save results. **Everything is free by default.** A local stand-in model (labelled as such) runs the same agent protocol, tool calls included, with no internet and no cost; `--offline` turns the model off entirely. The real Grok API is **billed per token by xAI**, so it is opt-in: add `--grok` (or `ACME_USE_GROK=1`). A saved key is never used otherwise. Each `--grok` run has a hard spending cap (`XAI_BUDGET_USD`, default $0.25) and the report shows the exact amount xAI says it charged.
Keep the key out of the repo: export `XAI_API_KEY` in your shell, or save it alone in `~/.config/acme_ap/xai_key` (`chmod 600`), where the code reads it. `python scripts/live_smoke.py` checks the live API; `tests/test_no_secrets.py` fails if a credential is ever committed.
Scanned PDFs need `tesseract` installed.

## What you will see

The run prints one row per invoice (PAID, REJECT, AWAITING approval, SCHEDULED, DUPLICATE), then a vendor-effort
scorecard and a payments summary. Side effects go to files, never to the outside world:

| Output | Where | What it is |
|---|---|---|
| Vendor follow-up questions | `outbox/*.txt` | Drafted, never sent automatically |
| Vendor status notices | `outbox/notices/` | Payment sent / approved and scheduled / with an approver |
| AP digest | `outbox/daily_digest.txt` | What is stuck, with whom, for how long |
| Approval decision cards | `outbox/approvals/` | One per invoice that needs a person |
| Ledger, payments, audit log | `inventory.db` (+ `.bank`) | Mock NetSuite and mock bank |

## Try the other flows

```bash
.venv/bin/python main.py demo                                              # the whole story in one go (scratch folder, offline)
.venv/bin/python main.py --invoice_path=data/emails                    # invoices arriving as emails
.venv/bin/python main.py --invoice_path=data/scanned                   # scanned PDFs (OCR, always held for a person)
.venv/bin/python main.py --invoice_path=data/approvals_demo --auto-vp  # over-limit invoice, simulated VP approves
.venv/bin/python approve.py list                                       # approver side: list, card, respond, tick
.venv/bin/python pay.py status                                         # treasury side: status, release, reconcile
.venv/bin/python main.py --trace --invoice_path=data/self_correction   # watch the agents work: model calls, tool calls, self-correction
.venv/bin/python main.py agents                                        # who does what, and when each may use the model
.venv/bin/python scripts/measure_llm_cost.py                           # model calls and tokens: ask-everything vs the gated team
.venv/bin/python main.py --bench 20000                                 # CPU benchmark
.venv/bin/python scripts/measure_quality.py                            # accuracy against the repo's labelled data
.venv/bin/python scripts/stress_extraction.py 500                      # thousands of corrupted invoices: any approved with a wrong value?
```

## Where things live

| Path | Responsibility |
|---|---|
| `main.py` | Command line (`demo`, a batch, `--bench`) |
| `acme_ap/runner.py`, `report.py`, `demo.py` | Run a batch and write the outbox; terminal tables; the guided demo |
| `approve.py`, `pay.py` | Approver and treasury command lines (act on the database a run leaves behind) |
| `acme_ap/agents/team.py` | The six agents and their charters (`python main.py agents`) |
| `acme_ap/pipeline.py` | The flow per invoice through the agents: ingest, deduplicate, validate, review, pay; CPU/IO scheduling |
| `acme_ap/ingestion/` | Turn any file into an `Invoice`: PDF/OCR, JSON/XML/UBL/CSV, regex and Grok extraction |
| `acme_ap/agents/` | `validation` (checks and findings) and `approval` (policy plus Grok draft/critique loop) |
| `acme_ap/approvals/` | Routing by amount and reason, signed one-click tokens, reminders, escalation, expiry |
| `acme_ap/payments/` | Scheduling, discounts, bank adapter (mock bank), account verification, reconciliation |
| `acme_ap/erp/` | `ErpAdapter` interface and the SQLite mock of NetSuite; swap in a real connector here |
| `acme_ap/intake.py`, `outreach.py`, `notices.py` | Read `.eml` files; draft follow-up questions, status notices and the AP digest |
| `acme_ap/metrics.py` | The numbers behind the `Impact` block |
| `acme_ap/models.py`, `llm.py` | Shared data types; Grok client and offline fallback |
| `data/` | Provided sample invoices, plus added sample emails, scans and an approval demo |
| `scripts/` | Sample generators and benchmarks |
| `tests/` | Pipeline, vendor-effort, scans, approvals and payments tests |

The one rule to know before changing agent code: the LLM may only make a decision stricter, never looser; the
deterministic policy has the last word (`acme_ap/agents/approval.py`).

## Documentation

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): ERP choice (NetSuite), vendor-channel strategy, CPU design and benchmarks, approval and payment design, known limitations
- [docs/ASSIGNMENT.md](docs/ASSIGNMENT.md): the original brief
