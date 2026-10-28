# Acme AP: invoice processing with a team of agents

Acme Corp loses about **$2M a year** to manual invoice processing: a 30% error rate, five-day delays, frustrated stakeholders. This is a working prototype that
reads invoices in any format, checks them against the ERP, gets the right person's decision where one is needed, pays on the due date, and tells everyone what
happened. Six agents with distinct jobs: Intake, Validation, Reviewer and Comms use Grok where reasoning helps (with tools and self-checks); Desk and Payment are deliberately rules-only, because a mistake there costs money. The order of the stages is fixed; the agents do not plan or delegate to each other. Built for the case in
[docs/ASSIGNMENT.md](docs/ASSIGNMENT.md).

## Quickstart

```bash
python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt   # Tesseract is only needed for scanned PDFs
python main.py --invoice_path=data/invoices                                              # the brief's command: all 17 sample invoices, free, no key
python main.py serve --demo                                                              # the web interface at http://127.0.0.1:8765
```

> **Which model runs?** By default, **Grok**, replayed: its answers come from real runs recorded on 2026-10-27 and 2026-10-28 against `grok-4.20-0309-non-reasoning`, shipped in this repository, so a run
> needs no key, no network and no money (the cost shown is what xAI reported when recorded). `--grok` calls xAI live (needs `XAI_API_KEY`, capped at $0.25 a run); `--simulate` is a local stand-in that is
> *not* Grok; `--offline` uses no model. An invoice of your own is not in the recordings, so the rules read it (the report says "N not recorded"): use `--grok` to put Grok on it.

<p align="center"><img src="docs/img/dashboard.png" alt="Dashboard: invoices, who is waiting, payments" width="720"></p>

<table><tr>
<td width="50%"><img src="docs/img/approval.png" alt="The approval page a decision link opens"><br><sub><b>The approver's page.</b> One screen: the plain-English summary, why it needs you, one click. The invoice is re-checked before any money moves.</sub></td>
<td width="50%"><img src="docs/img/invoice-trace.png" alt="Invoice page with the agents' trace"><br><sub><b>What each agent did.</b> Here a mistyped item (<code>Wdgt B</code>) is found by a catalogue search tool, corrected, re-validated, and paid.</sub></td>
</tr></table>

<p align="center"><img src="docs/img/upload.png" alt="Upload page" width="560"><br><sub><b>Upload.</b> Drop PDFs, text, JSON/XML/CSV or emails; they go through the same agents and the same ledger as the command line.</sub></p>

The command line is first-class too: [`docs/img/demo.svg`](docs/img/demo.svg) is the real output of `python main.py demo`.

## What the brief asked for, and where it is

| The brief says | Here |
|---|---|
| Run `python main.py --invoice_path=...` with structured logs and results | exactly that. Decisions and payments are always logged as `key="value"` lines (`acme.decision`, `acme.payment`); **`-v` adds the per-stage logs** (ingest, each finding). Example: `acme.payment Paid 5000.00 to Widgets Inc.`. `--json FILE` writes the full results |
| A local SQLite inventory the validation stage checks | **`inventory.db`**, created on the first run (`--db` to move it) with the brief's four rows (`WidgetA` 15, `WidgetB` 10, `GadgetX` 5, `FakeItem` 0) plus unit prices and an approved-vendor table: `acme_ap/erp/mock_netsuite.py` |
| Flag stock mismatches, unknown items, invalid data | `insufficient_stock`, `out_of_stock`, `unknown_item`, `invalid_quantity` and more: `acme_ap/agents/validation.py` |
| **Invoices over $10,000 need additional scrutiny / VP approval** | above 10,000 a person signs off however clean the invoice is: `APPROVAL_THRESHOLD = 10000` in `agents/validation.py` and `VP_THRESHOLD = 10000` in `approvals/routing.py` (CFO above 50,000) |
| Approval agent with a reflection or critique loop | draft, then critique, then revise, up to 3 rounds: `acme_ap/agents/approval.py` |
| **`mock_payment(vendor, amount)`** if approved; log the rejection with reasoning if not | `acme_ap/payments/mock.py` is the brief's function, unchanged in behaviour; the simulated bank calls it to move the money (`payments/bank.py`). A rejection is logged with its reasoning and kept in the audit log |
| Grok as the reasoning engine; function calling, structured outputs, self-correction | `acme_ap/llm.py` (xAI's OpenAI-compatible API, tools, strict JSON Schema); which model actually runs by default is explained above |
| A multi-agent orchestration (framework or custom) | custom, `acme_ap/pipeline.py` and `acme_ap/agents/team.py`; `python main.py agents` prints the roster |

## Results

| What | Mode | Result | Evidence |
|---|---|---|---|
| The 17 provided invoices | rules only (`--offline`) | **17 / 17 end as labelled**, 8 / 8 on the outcomes the brief itself states, 0 wrongly paid: 7 paid, 8 rejected, 2 sent to a person, 3 duplicates blocked | **[docs/EVALUATION.md](docs/EVALUATION.md)**: every invoice, decision and reason, the two modes reported separately |
| The 17 provided invoices | Grok, replayed from a live recording (the default) | the same 17 / 17, 0 errors; 36 model calls and 42 tool calls; **$0.0208 total (about $0.0012 per invoice)**, the cost xAI reported when it was recorded live; the model changed no decision | [`docs/live_runs/`](docs/live_runs/): transcript, JSON and the recording (`--replay-llm`) |
| Reading 58 messy invoices (12 layouts written by Grok, answers fixed beforehand) | rules only | **48%** fully right (28 / 58); 0 wrong invoices would have been approved | [docs/EXTRACTION_EVAL.md](docs/EXTRACTION_EVAL.md) |
| Reading the same 58 | Grok when the rules are in doubt, replayed | **58 / 58** fully right; 33 model calls, $0.031 | [docs/EXTRACTION_EVAL.md](docs/EXTRACTION_EVAL.md) |
| Reading 30 degraded scans (through OCR) | rules only / Grok, replayed | **63%** / **83%** | [docs/SCAN_EVAL.md](docs/SCAN_EVAL.md) |
| **Red team** | rules only, and Grok replayed (both reported) | 13 fraud and failure scenarios (look-alike vendor, split purchase, re-numbered duplicate, inflated prices, hidden instructions to the AI, ...) plus 3 clean control invoices: **13 / 13 stopped, 3 / 3 still approved**, with rules only and with the recorded live Grok run. The first run stopped 7 / 13 and live Grok once *invented a shipping charge* to repair a fake total: both are fixed and pinned by tests | **[docs/RED_TEAM.md](docs/RED_TEAM.md)**, `python scripts/red_team.py` |
| Errors caught before payment | rules (no model involved) | stock shortfalls, unknown items, arithmetic that doesn't add up, a fraud pattern, a changed bank account, duplicates across formats | `data/invoices`, `tests/test_brief.py` |
| Robustness to garbage | rules only | 2,000 randomly corrupted invoices: **0 approved with a wrong value** (a stray "see invoice 31337" once got through; found and fixed) | `scripts/stress_extraction.py` |
| Speed | no model (`--offline`) | a clean invoice spends about 10-15 ms in the system, against "5 days" in the brief; about 2,200 documents/s on 14 cores | `main.py --bench`, [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) §4 |
| Waiting on a person | no model | never silent: reminder at 24 h, escalation at 48 h, expiry at 5 days | `approve.py tick`, `make e2e` |
| Tests | no model | **351 tests** plus a **60-check end-to-end run** from a fresh clone; a test fails the build if a credential is ever committed | `make test`, `make e2e` |

## Business impact

| | Before (from the brief) | Now | How we know |
|---|---|---|---|
| **Error rate** | 30% of invoices | on the provided set, all 10 problem invoices were stopped or sent to a person before any money moved, **0 wrong payments** (the brief itself names 5 of them: a stock mismatch, out-of-stock/fraud, two unknown items, invalid data) | `docs/EVALUATION.md`; 2,000-invoice stress test |
| **Time per invoice** | 5 days | clean invoices: milliseconds, no human. Invoices needing a person: a prepared one-click card, with a clock that reminds at 24 h and escalates at 48 h | `main.py` impact block, `approve.py tick` |
| **Cost per invoice (model)** | n/a | **about $0.0012** measured live, roughly **$122 a year at 100,000 invoices**; free to run: the model answers come from the recording | `docs/live_runs/`, `scripts/roi.py` |
| **People's time** | 12 min of keying and chasing (assumed) | only the invoices that need a decision, and those arrive summarised | `scripts/roi.py` (your inputs) |

The savings side depends on Acme's volume and wages, which the brief does not give, so `python scripts/roi.py` takes your numbers and shows the structure and a sensitivity table. With *placeholder*
inputs (100,000 invoices, 12 min each, $45/h, 30% errors at $60) today's cost comes out in the millions, the same order as the brief's $2M. Those inputs are assumptions, not Acme's figures; only
the cost side is measured.

## Design decisions

1. **The model proposes, the rules dispose.** A model can make a decision stricter, never looser; it never touches payments, routing or bank details; text it writes is checked against the
   record. *Trade-off:* on clear-cut invoices the model rarely changes an outcome (see EVALUATION.md); its value is second opinions, plain-English notes, reading messy documents and fixing mistyped names.
2. **Ask the model only where it can matter.** Never on rejects or anything already with a person; once before each payment; in extraction only when the rules' reading is incomplete. *Trade-off:* a cheaper,
   safer system that looks less "AI-heavy"; asking about everything is ~2.5x the calls (`scripts/measure_llm_cost.py`).
3. **A custom orchestrator, not LangGraph/CrewAI.** CPU-bound PDF work and I/O-bound model and bank calls need different scheduling, and six agents with charters did not need a framework. *Trade-off:* more code of our own.
4. **Grok by default, replayed; live on request, capped.** Out of the box the model is Grok: its answers come from recordings of real runs that ship in the repository (the provided invoices, the red-team documents, the extraction corpus, the self-correction samples), so a reviewer needs no key, no network and no money, and sees Grok's own wording, tool calls and corrections. A document the recordings do not cover (your own invoice) is read by the rules, and the report says how many questions were not recorded. `--grok` asks live Grok (opt-in, per-run budget); `--simulate` is the old local stand-in; `--offline` is no model; the default model is a plain one, not a reasoning one, because a
   reasoning model was ~3x the cost and several times slower for this work (measured). *Which name:* the client asks for `grok-3` by default, but on the account used here that name was served by a slower, dearer reasoning model (`grok-4.3`), so with no `XAI_MODEL` set it asks the API what the key can use and picks a non-reasoning model; every recording was made with `grok-4.20-0309-non-reasoning`. Set `XAI_MODEL` to force one.
5. **The ledger is the system's memory and every way in keeps it** (the command line, the web page, the demo, and the classes' own defaults; only `--reset` or a scratch database made for one run starts empty), so nothing is ever paid twice, with idempotency keys on every payment. Duplicates use the vendor's master name; a changed resubmission of a rejected invoice is allowed.
6. **Stock is a per-invoice cap by default** (the brief's own table lists three invoices as "within stock" that together exceed it); `--stock-policy reserve` is the cumulative alternative, and `--stock-policy decrement` takes the units off the shelf for good when an invoice is paid (kept between runs; `main.py stock` shows what is left).
7. **Payments are scheduled, not instant**: due date minus bank lead time (business days), early-pay discounts captured, an account on the invoice that differs from the one on file is held, new accounts need two people.
8. **Approvals are signed links issued to one mailbox**, single-use, expiring, bound to the invoice as shown, with the signing key outside the database. *Trade-off:* a forwarded link acts as its addressee; real use needs email delivery and SSO.
9. **A small web interface on the standard library, not Streamlit**: upload, dashboard, review queue and the approval page in one dependency-free server (hardened for the endpoints that can pay). *Trade-off:* less polish than a UI toolkit.
10. **Evidence over claims**: live runs are recorded and replayable, the evaluation reports rules-only and LLM-on separately, and labels written by the developer are kept apart from the brief's own.

## How it works

```
document -> INTAKE -> VALIDATION -> REVIEWER --approve--> PAYMENT -> COMMS
 (any format)   |          |  (resolver)    |  \--escalate--> DESK (a person decides) --approve--> PAYMENT
                |          |                \--reject---------------------------------------------> COMMS
```

| Agent | Job | Uses the model? |
|---|---|---|
| **Intake** | any document (PDF, scan, email, JSON/XML/UBL/CSV, text) into a structured invoice | only when the rules' reading is in doubt; answer cross-checked field by field |
| **Validation** | vendor, items, stock, arithmetic, dates, fraud signals, bank account; a *resolver* searches for mistyped item names and re-checks | rules; a model-driven search loop only for names it cannot match |
| **Reviewer** | approve / escalate / reject; a model with read-only tools gives a second opinion, a challenger audits it | yes, before every payment |
| **Desk** | the human in the loop: right approver, signed one-click link, reminders, escalation | **never** |
| **Payment** | schedule to the due date or capture a discount, verify the payee account, send idempotently, reconcile | **never** |
| **Comms** | vendor questions, plain-English notes, remittance notices, the AP digest | to phrase only; checked against the record |

### What the model actually does (the agentic part)

| Capability | Where | Proof |
|---|---|---|
| **Function calling** | the reviewer's model calls `lookup_item`, `lookup_vendor`, `vendor_history`, `policy_limits`; the resolver calls `search_items`, `search_vendors`; bad calls come back as errors it can correct from | `tests/test_tooluse.py`, `scripts/live_smoke.py`, 42 live tool calls in `docs/live_runs/` |
| **Native structured output** | replies must fit a Pydantic schema, sent to the API as strict **JSON Schema** (stepping down to JSON-object mode, then plain, only if an endpoint refuses); JSON is parsed, never grepped | `tests/test_tooluse.py`, live-verified |
| **Self-correction loops** | arithmetic self-check and retry; a malformed reply is returned to the model with the reason; the auditor challenges the reviewer's draft; **tool-driven item correction** re-validates after a fix; the always-on draft-critique-revise reflection | `tests/test_agent_upgrades.py`, `tests/test_brief.py` |
| **Guards on the model** | item fixes only if the pick exists *and* is textually close; vendor names are never auto-corrected, only flagged (look-alike vendors are a classic fraud) | `tests/test_agent_upgrades.py` |
| **Visible reasoning** | every invoice records which agent did what, which tools the model called and what it concluded | `--trace`, the web page, `--json` |

## Run it

```bash
python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt   # set up (Tesseract only for scanned PDFs)
python main.py --invoice_path=data/invoices                   # process the sample invoices on Grok's recorded answers (no key, free, offline)
python main.py --invoice_path=data/invoices --reset           # run again from an empty ledger (without --reset the ledger is kept: nothing is ever paid twice)
python main.py serve --demo                                   # the web interface: dashboard, upload, review queue (http://127.0.0.1:8765)
python main.py --simulate --invoice_path=data/invoices         # the same, on the local stand-in model instead of the recording
python main.py demo                                           # the whole story in one command
python main.py --replay-llm docs/live_runs/provided-invoices.cassette.jsonl --invoice_path=data/invoices   # a real Grok run, replayed offline for free
python main.py --trace --invoice_path=data/self_correction    # watch the agents work
python main.py --grok --invoice_path=data/invoices            # real Grok: billed per token, capped per run
make test        make lint        make e2e        make ui
```

**Cost safety.** Everything above except `--grok` and `--record-llm` is free. They use the real xAI API, billed per token; they need `XAI_API_KEY` or a key saved alone in
`~/.config/acme_ap/xai_key` (`chmod 600`), and a key never gets used just because it exists. Each run stops asking the model at `XAI_BUDGET_USD` (default $0.25) and the report shows the exact
amount xAI says it charged. The key is never written to the repo, logs or outputs; `tests/test_no_secrets.py` fails the build if one is committed.

Contributing and a map of the code: [CONTRIBUTING.md](CONTRIBUTING.md). More: `approve.py` and `pay.py` (approver and treasury command lines), `main.py agents` (the roster), `--bench 20000`, `scripts/evaluate.py`, `scripts/measure_quality.py`, `scripts/stress_extraction.py`.

## The brief's evaluation criteria

| Criterion | Where to look |
|---|---|
| **Functionality** | 17/17 end to end, offline and on live Grok; `make e2e` (60 checks from a fresh clone) |
| **Code quality** | typed models, one responsibility per module, error handling at every boundary (a failing model step never fails an invoice), structured logs, 351 tests |
| **Agentic sophistication** | the table above: tool use, strict structured output, five self-correction loops, six agents with charters, guards; recorded, replayable live runs and a per-invoice evaluation |
| **Shipping mindset** | simulated what must be (bank, ERP, email) behind adapters; cut what doesn't earn its keep (no framework, no database server, no auth system); limits stated in §5 of the architecture doc |
| **Presentation** | this page; measured results with evidence; business impact, cost and ROI; design decisions; screenshots from a real run |
| **Above and beyond** | a red-team suite that attacks the system (and found real holes), "what would get this paid" to-do lists on every stopped invoice, email and e-invoice intake, OCR, signed one-click approvals with escalation, scheduled payments with discounts and bank holidays, bank-account fraud check with dual control, reconciliation, adversarial and stress tests, a cost guard, record/replay |
| **UI/UX** | the web interface (dashboard, **upload**, review queue, approval page, per-invoice agent trace), a guided demo, readable CLI output, links that actually open |

## Where things live

| Path | Responsibility |
|---|---|
| `main.py` | command line: a batch, `demo`, `serve`, `agents`, `--bench`, `--record-llm` / `--replay-llm` |
| `acme_ap/web.py`, `store.py` | the web interface (standard library only) and the record it reads |
| `acme_ap/agents/` | `team.py` (the six agents), `validation`, `resolver`, `approval`, `comms`, `tools` |
| `acme_ap/pipeline.py`, `parsepool.py` | the stages one invoice goes through (ingest, validate, decide, pay, notify, settle); reading documents on a process pool |
| `acme_ap/llm.py`, `cassette.py`, `sim_llm.py` | Grok client (tools, schema output, retries, cache, budget), record/replay, and the local stand-in |
| `acme_ap/ingestion/` | any file into an `Invoice`: PDF/OCR, JSON/XML/UBL/CSV, regex and Grok extraction |
| `acme_ap/approvals/`, `payments/`, `erp/` | routing and signed links; scheduling, bank adapter, reconciliation; the ERP interface and its SQLite mock |
| `data/`, `scripts/`, `tests/` | provided and added samples; benchmarks, evaluation, screenshots, ROI; the tests |

## Limits worth knowing

The bank, ERP and email are simulated behind adapters; no email is sent. A decision link is issued to one approver's mailbox and whoever holds it acts as that person; a real deployment must deliver it by email
and put single sign-on in front of the page. Stock is checked per invoice by default (the brief's own sample table requires that). On the provided invoices Grok agrees with the rules, so the evaluation shows
what the model adds (second opinions, notes, emails) rather than different decisions; where it changes outcomes is `data/self_correction/`, which is the developer's own sample set. The extraction corpus was written by a model, not collected from real suppliers, so real-world accuracy will differ; scans reach 83% with Grok, not 100%, which is why a doubtful reading goes to a person. Not tested on Windows itself. Details: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) (ERP choice, vendor strategy, CPU design, approval and payment design, hardening, live verification, review fixes, limits).
