# Working on this code

## Set up, test, lint

```bash
make setup      # venv + dependencies (Tesseract is only needed for scanned PDFs)
make test       # 351 tests, about 30 s, no network, no API key
make lint       # ruff + mypy (mypy is a smoke check, not a gate)
make e2e        # 60 end-to-end checks, as a user would run them
```

## Start reading here

Follow one invoice: `main.py` -> `acme_ap/runner.py` -> `acme_ap/pipeline.py`. The pipeline is a short list of stages
(`_ingest`, `_validate_and_decide`, `_pay`, `_notify_and_settle`); each calls one agent in `acme_ap/agents/team.py`, and
`CHARTERS` at the top of that file says what each agent is for and when it may use the model.

| Folder / file | What lives there |
|---|---|
| `acme_ap/parsepool.py` | reading documents: the process pool for PDFs, inline for cheap formats |
| `acme_ap/ingestion/` | any file -> `Invoice`: `heuristic.py` (regex reader), `llm_extract.py` (model reader + cross-check), `ocr.py`, `structured.py` (JSON/XML/CSV/UBL) |
| `acme_ap/agents/` | the agents: `validation.py` (rules), `resolver.py` (catalogue search), `approval.py` (policy + model critique), `comms.py` (wording) |
| `acme_ap/erp/` | `base.py` is the interface; `mock_netsuite.py` is the SQLite stand-in. A real ERP is one more class here |
| `acme_ap/payments/` | `service.py` (scheduling, limits, reconciliation), `bank.py` (bank adapter + mock) |
| `acme_ap/approvals/` | routing table, signed links, reminders |
| `acme_ap/llm.py`, `cassette.py`, `sim_llm.py` | the Grok client, record/replay, and the free local stand-in model |
| `acme_ap/redteam.py`, `nextsteps.py` | the attack catalogue and its runner; the "what would get this paid" to-do list for each stopped invoice |
| `acme_ap/web.py`, `report.py`, `demo.py` | web interface, terminal output, the guided demo |
| `data/`, `docs/` | sample invoices; architecture, evaluation and live-run evidence |

## Plugging in something real

* **ERP:** implement `ErpAdapter` (`acme_ap/erp/base.py`) and pass it to `Pipeline`. The pipeline only calls that interface.
* **Bank:** implement the adapter in `acme_ap/payments/bank.py`. Payments are idempotent by key; keep that.
* **Email:** `acme_ap/notices.py` and `outreach.py` produce the messages; sending them is the missing piece.
* **Model:** `acme_ap/llm.py` speaks the OpenAI-compatible protocol, so another provider is a base URL and key.

## Rules the code relies on

* The model may make a decision stricter, never looser. Anything it writes is checked against the record before use.
* Never commit a credential: a test (`tests/test_no_secrets.py`) fails the build. The xAI key lives in `~/.config/acme_ap/xai_key`.
* If you change a prompt, the recordings in `docs/live_runs/` and `data/extraction_eval/` no longer match; the replay tests will say so. Re-record with `--record-llm` (live, costs cents).
* New fraud rule? Add the attack to `acme_ap/redteam.py` first (it should fail), then the rule, then run `python scripts/red_team.py`. A new finding code also needs a line in `acme_ap/nextsteps.py` (a test checks).
* Evaluation files are generated; `scripts/evaluate.py --check` (and the extraction and scan equivalents) fail when they are stale.
