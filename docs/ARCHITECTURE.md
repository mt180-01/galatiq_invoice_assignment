# Acme AP: target architecture, ERP choice, vendor channels, CPU design

Scope note: everything below that touches NetSuite is built against a **local mock** (`acme_ap/erp/mock_netsuite.py`)
behind an adapter interface. No real NetSuite account was used. Benchmarks are from one 14-core laptop.

## 1. From legacy + manual to AI-native

| Today | Target | In this repo |
|---|---|---|
| Invoices arrive as PDFs in an inbox; staff re-key them | Intake mailbox/portal/EDI feeds one queue; extraction is automatic | `ingestion/` (structured lane + regex + Grok extraction with self-check) |
| Legacy inventory DB: inconsistent, costly, hard to maintain | A cloud ERP is the single system of record for items, vendors, bills, payments | `erp/` adapter + SQLite mock |
| Approval by email chains | Policy engine + LLM reviewer with a critique loop; humans only get exceptions | `agents/approval.py` |
| Payment keyed by hand | Payment agent behind a single gate; idempotent via the bill ledger | `agents/payment.py` |

**Replace vs. integrate.** Do not rip out everything at once. Recommended order, each step shippable on its own:
1. **Wrap** the legacy DB behind the `ErpAdapter` interface and run the AI pipeline in shadow mode (decisions logged, nobody paid by it).
2. **Cut over reads/writes** to the new ERP for item and vendor master (migrate and de-duplicate the inconsistent data first: this is the real work).
3. **Turn on auto-pay** for the clean, low-value slice; widen as the exception rate proves out.
4. **Decommission** the legacy DB once nothing reads it.

Pipeline: `ingest -> dedupe claim -> validate -> approve (draft/critique/revise) -> pay -> settle`. The model never writes to the ERP directly:
it proposes, deterministic code disposes (an LLM can make a decision stricter than policy, never looser; tested in `test_llm_cannot_loosen_policy`).

## 2. NetSuite vs. Dynamics 365 vs. SAP S/4HANA

**Choice: NetSuite**, under these assumptions about Acme (stated, not given in the brief): mid-market PE-backed manufacturer,
modest IT team, owners that want measurable savings within a year or two of the deal, no existing SAP or Microsoft ERP estate.

| | NetSuite | Dynamics 365 Finance & SCM | SAP S/4HANA |
|---|---|---|---|
| Fit for mid-market manufacturer | Strong; inventory, AP, PO, vendor bill in one suite | Strong, especially inside a Microsoft shop | Strongest for large/complex multi-entity manufacturing |
| Time and effort to implement | Typically the lightest of the three | Medium | Typically the heaviest and longest |
| Fits a PE value-creation clock | Best fit | Good | Often too slow/costly unless the group already runs SAP |
| Integration surface for AI | REST (SuiteTalk), SuiteQL, SuiteScript, OAuth 2.0 | Dataverse/OData, Power Platform | OData/APIs, BTP; more specialist skills needed |
| Multi-tenant SaaS, upgrades handled by vendor | Yes | Yes | Cloud editions yes; heavier customisation tends to creep in |

Why NetSuite wins *here*: the problem is a small, well-defined AP/inventory loop at a company that needs speed and low run cost,
not SAP's depth. **When the answer flips:** an existing Microsoft estate favours Dynamics 365; a parent group on SAP, heavy
multi-plant/regulatory complexity, or very high volumes favour S/4HANA. These are qualitative judgements from general knowledge;
validate with vendor quotes and a reference check before committing. I did not verify current pricing.

## 3. Will big vendors fill in a form instead of uploading a PDF?

Honest answer: **mostly no, if "using the form" means a person retyping an invoice into a portal.** That is *more* work for them
than attaching the PDF they already generated. Large vendors will comply only when it costs them nothing extra:

- **Machine-to-machine** (EDI, PEPPOL/UBL e-invoicing, or an API push from their billing system): set up once, then free. They often already do this for other customers.
- **Portal as a fallback, not a requirement**, with carrots: faster payment or an early-pay discount for structured submissions, and PO-flip invoicing (the vendor confirms a prefilled PO instead of typing anything).
- **Never reject a PDF.** Small vendors will keep emailing PDFs; AI extraction is the safety net.

### What the system does to keep vendor effort near zero

The principle: **every minute of effort sits on our side, and the vendor keeps doing what they already do.**

| Vendor does | We do | Implemented |
|---|---|---|
| Emails a PDF (today's habit) | Read it (layout-aware text, regex, optional Grok with a cross-check) | yes (`ingestion/`) |
| Emails several invoices in one message, or types it in the body | Split attachments / read the body, keep the sender | yes (`intake.py`, `.eml`) |
| Sends a normal-looking PDF that has e-invoice XML inside (Factur-X / ZUGFeRD / UBL) | Read the embedded XML: no AI, no guessing | yes (`embedded_einvoice`) |
| Sends UBL / CII XML, JSON, CSV | Parse deterministically | yes (UBL and CII parsers are tested against hand-written samples only, not official PEPPOL/Factur-X conformance files) |
| Made a fixable mistake (total does not add up, missing vendor name, unknown item) | Draft **one** short email with at most 3 questions, each answerable in one line; no portal, no login | yes (`outreach.py`, drafts only) |
| Did something suspicious | Do **not** reply to the sender; send to a human | yes |

Questions we deliberately do not ask vendors: stock shortfalls, FX/currency policy, approvals. Those are our problems.
Vendor-side defence: XML is parsed with `defusedxml`, since vendor files are untrusted.

Outbound levers that need a business decision, not code (and are not built): publish a one-page "how to invoice us" with a PEPPOL ID/EDI address; offer earlier payment on machine-readable invoices;
PO-flip (we send a prefilled PO, the vendor confirms) for the biggest vendors. The scorecard printed after every run
(`machine-readable / AI-read / follow-up needed`) is the KPI for whether those levers work.

Running it on the 17 distinct sample invoices: 59% arrive as structured files, 41% need PDF/text extraction,
and 4 (24%) would get a follow-up question (INV-1007, 1009, 1013, 1016). INV-1013 is a real catch: its lines sum to 21,040,
tax is 1,472.80, so the total should be 22,512.80, but the invoice says 22,562.80. Sample emails (the repo ships none) are
generated by `scripts/make_sample_emails.py` into `data/emails/`.

## 4. CPU design

Work is split by what it is bound on:
- **CPU-bound** (PDF layout analysis): `ProcessPoolExecutor`, so it uses all cores (Python's GIL rules out threads for this).
- **I/O-bound** (Grok, ERP): `asyncio` with a bounded semaphore, so waiting on the network never blocks a core.
- **Per-document chaining, no stage barriers**: when a worker finishes parsing, the next stage starts for that document while the core moves on.
- **Routing by cost**: cheap formats (~0.05 ms) are parsed inline; only PDFs (~3 ms) go to the pool, because IPC would cost more than the parse.
- **Single writer**: SQLite in WAL mode, writes serialised, one transaction per invoice decision.
- **Pool size is auto-planned** (`PDFS_PER_WORKER`): each worker costs ~0.7 s to start, so tiny batches run serially.

Measured with `python main.py --bench N` (50% PDFs, offline mode, 14 cores):

| Docs | Serial | Parallel | Speedup | Core utilisation (parallel) |
|---|---|---|---|---|
| 20,000 (current: with approvals + payments) | 1,032 docs/s | 2,318 docs/s (auto mode: 2,436) | 2.2x | 34% |
| 20,000 (earlier, before approvals/payments existed) | 1,240 docs/s | 3,142 docs/s | 2.5x | 41% |

The current run is about 17-26% slower than the earlier one: every approved invoice now also writes a payment instruction and
calls the bank adapter, and escalated ones open an approval request. Re-measuring found two real bugs that this benchmark exposed and
that are fixed and covered by regression tests: the daily-limit check re-read every payment sent that day (quadratic: the run did not finish in
13 minutes), and the approval service shared one SQLite connection across threads and corrupted reads under parallel load (hundreds of failed invoices).

**This is not linear scaling, and cores are not fully busy.** The ceiling is the single orchestrating process (result pickling,
validation, SQLite commits): Amdahl's law. The next step is horizontal: a durable queue (SQS/Kafka) with N independent worker processes,
each owning its own pool, and the ERP as the shared store. In the real system the dominant latency is the LLM and the ERP API, not CPU, which is why the async layer matters more than the pool.
With `XAI_API_KEY` unset (or `--offline`) the agents run deterministically; the Grok path is covered by mocked-transport tests only and has **not** been run against the live API.

### SIMD: where it helps and where it does not

I measured each stage before adding anything (`scripts/bench_simd.py`, plus per-stage timings on this arm64 machine, numpy using NEON):

| Stage | Cost per document | SIMD / vectorisation verdict |
|---|---|---|
| PDF text read (MuPDF, C) | ~1.9 ms | already native code; nothing to add |
| Row clustering of ~70 words | 7.7 us Python vs 20 us numpy | numpy is **slower** (arrays too small); not used |
| Validation arithmetic | 10-40 us | too small to matter; batching it would add latency for under 5% |
| Regex extraction | 90-200 us | `re` is not vectorisable |
| **Scanned-page clean-up** (4 MP of pixels) | **~70 ms** | **yes: whole-array numpy ops** |

Scanned pages are the one place with enough data per item. `ingestion/ocr.py` flattens uneven scan lighting, thresholds (Otsu) and erases
table rules with whole-array numpy operations, then hands the page to Tesseract. Measured: the normalise+threshold step is ~27x faster than a
per-pixel Python loop (9 ms vs ~250 ms extrapolated from a crop), and the full clean-up is ~12% of Tesseract's time. It also makes Tesseract
faster (one sample went from 1.9 s on the raw scan to 0.5 s on the cleaned one). Not every numpy step is SIMD (cumulative sums and `bincount` are scalar).
Tesseract, not the vector code, is the cost that matters, which is why OCR runs in the process pool.

**OCR accuracy is limited and policy reflects that.** On my synthetic scans (the repo ships none; `scripts/make_scanned_sample.py`), headers and totals are read
well but table rows are only partly recovered (e.g. 4 of 8 lines on one invoice). So OCR output is never auto-paid: it is held for a person
(`ocr_source`), and the vendor is not asked to explain numbers we may have misread. Real scans, skew, and stamps will be harder than these samples.

Also found while measuring: the PDF was opened twice per document. Opening once cut PDF parse from ~2.7 ms to ~1.9 ms, but end-to-end throughput
did not change (the single orchestrating process is still the ceiling).

## 4b. VP approval: replacing the email chain

**Should it be automated?** Partly, and the split is deliberate. Approval is a fraud control, so the system automates the clerical work around the decision, not the decision itself where money or risk is high.

| Situation | What happens |
|---|---|
| Clean, <= 10,000 | Paid automatically (the rule-based approver; unchanged) |
| Clean but > 10,000 | **A person signs off.** Previously the AI approved these; that removed the control the brief asks for, so it was changed. |
| Flagged (new vendor, foreign currency, revision, OCR, pressure language) | Routed to the right owner with the evidence |
| Any hard error (stock, math, unknown item) | Rejected; no approval request exists, so nobody gets an approve button for something known to be wrong |

**The workflow** (`acme_ap/approvals/`):
- **Routing**: amount over 10k -> VP Finance, over 50k -> CFO; unknown vendor -> Procurement; foreign currency -> Treasury; fraud-style/OCR/revision -> AP Manager. Unreadable documents have nothing to approve, so no request is created.
- **Decision card**: one short message per invoice (why you, the invoice, the system's recommendation, anything noted) with Approve and Reject links. Replaces the thread.
- **Signed links**: HMAC-SHA256 token bound to the request, the action, a hash of the invoice content, and an expiry. Altered tokens fail; a link dies if the invoice changes after the card was sent; links are single-use.
- **Re-check at approval time**: before any payment, validation runs again. If stock or vendor status changed since the card went out, payment is blocked even though a person clicked approve.
- **Clock-driven**: reminder at 24 h, escalation to the delegate at 48 h (VP Finance -> CFO -> CEO), expiry at 5 days. Expiry never pays.
- **Audit**: every step (opened, reminded, escalated, approved/rejected/blocked/expired, by whom, comment) is an event row, and the ERP bill status follows.

Try it: `python main.py --invoice_path=data/approvals_demo`, read `outbox/approvals/*.txt`, then `python approve.py list` / `respond <token> --as <email>` / `tick --hours 50`.
`--auto-vp` runs a clearly labelled **simulated** VP for demos. The brief's "simulate VP review" is covered by the rule-based approver plus this workflow; no real person or mail system is involved.

**Not built / limits**: no email sending, no inbound reply parsing, no web endpoint (the links point at a fictional host and are exercised through `approve.py`), no authentication of the responder beyond the signed token (a real endpoint must authenticate the person, e.g. SSO, and check they hold the role), no segregation-of-duties check against who created the bill, no out-of-office calendar, no bulk approval. The approver directory is fictional role mailboxes.

**SIMD / vectorisation: not used here, on purpose.** Measured (`scripts/bench_approvals.py`): routing 0.8 us, hashing 2.6 us, signing/verifying a token ~2.5 us, and creating a request plus rendering its card ~100 us. A person takes minutes to days.
Even 100,000 pending requests is ~10 s of compute in total. The cost to optimise is human waiting time (right person, evidence in the message, reminders, escalation), not CPU.

## 4c. Payment: from "call a function" to a safe, scheduled, reconciled bank transfer

**What was wrong:** money moved before the ledger recorded it; the reference was `sha1(vendor|amount)` (two different
$500 bills from one vendor collided); nothing checked *where* the money went; everything was paid on day 0; one
blocking call per invoice with no retry; no lifecycle after "paid".

| Gap | Now |
|---|---|
| Crash between bank call and ledger | Instruction is written first, keyed by the bill (idempotency key). Before any submit we ask the bank what it holds for that key (`recover`), so nothing is paid twice. |
| No retry / outage handling | 3 attempts with backoff per release; transient errors leave it `scheduled` and the next `release` finishes it; permanent rejections stop at once and go to a person. |
| Pays on day 0 | Released on due date minus the rail's lead time (ACH 2d, wire same day). Overdue or no terms: now. |
| Early-pay discounts ignored | "2/10 Net 30" is parsed; the discount is taken while the window is open (about 36% annualised, so worth it). |
| Invoice asks for a different bank account | Parsed from JSON/XML/CSV/UBL/CII/text. A mismatch with the account on file is a HOLD (`bank_account_changed`): routed to the AP Manager, the vendor is **not** emailed to confirm (the sender may be the fraudster), account numbers are masked in messages, and an approver's click cannot waive it. The fix is the dual-control account change below. Invoices that show no account are paid to the one on file. |
| Unknown payee account | Each vendor needs a *verified* account. A changed account needs a second person and holds that vendor's payments meanwhile. |
| One rail | Wire at/above 25,000, ACH below. |
| Uncontrolled outflow | Daily outbound limit enforced under the ledger lock; overflow rolls to the next day. |
| ERP and bank disagree after a crash | `reconcile` also repairs a bill the bank has paid but the ERP never marked paid (and says so). |
| Zero or negative amounts | Validation errors (`zero_total`, `negative_total`); the payment layer refuses them again on its own. |
| "Paid" is a guess | States: scheduled, submitted, settled, returned, held, failed. `reconcile` compares ledger and bank: settles, flags returns, flags bank items we have no record of and amount mismatches. |
| CPU / latency | Bank calls run off the event loop under a semaphore and outside the ledger lock, so submissions overlap. No SIMD: this stage is network- and DB-bound. |

Files: `acme_ap/payments/` (`service.py` policy and ledger, `bank.py` adapter + simulated bank, `terms.py`), CLI `pay.py`.
Policy numbers (25,000 wire threshold, 250,000 daily limit, 0.30 / 25.00 rail costs) are assumptions.
**Deliberately not done:** stock is not reduced when an invoice is paid. These are purchase invoices, so paying one does not consume inventory (receiving goods would add to it), and a decrement would make results depend on processing order.
**Not done:** no real bank (the adapter is the seam; a real one adds credentials, request signing, webhooks instead of polling);
weekends/bank holidays ignored; no sanctions screening; no remittance-advice email to the vendor; FX not supported;
the verification "callback" is a second person's name in a CLI, not a phone call.

## 4d. The three pain points, and how each is addressed and measured

| Pain point (from the brief) | What causes it | What the system does | How it is measured here |
|---|---|---|---|
| **30% error rate** | Hand-keying from PDFs; nobody checks stock, math, duplicates or the vendor | Structured formats are parsed, not re-typed; PDFs/text are read by Grok with a regex cross-check (disagreement = hold); every invoice is checked against items, stock, vendor master, arithmetic, duplicates, fraud signals and (new) the vendor's bank account; uncertain cases go to a person instead of being guessed | `python scripts/measure_quality.py`: 17/17 provided invoices end as labelled, 0 wrongly paid; 25/25 printed fields on the three PDF/text twins match their structured copy |
| **5-day delays** | Invoices wait in someone's inbox; approvals are email chains nobody chases | Clean invoices take no human time (median tens of ms in the system); approvals go to the right person as a one-click card with a 24h reminder, 48h escalation and 5-day expiry; payment is timed to the due date, not to a backlog | `Impact` block after each run; `approvals.waiting()` gives age, owner and next step for everything pending |
| **Frustrated stakeholders** | Vendors chase for status, approvers get unclear emails, AP does not know what is stuck | Vendor notices (remittance advice with the real amount, scheduled date, or "with X for sign-off, decision within 2 business days"), one question per invoice at most, and an AP digest of what is stuck, with whom, for how long, and what happens next | `outbox/notices/`, `outbox/daily_digest.txt` |

**What this does not prove.** The 30% and 5-day baselines are the brief's, not measured here, so no "reduced by X%" claim is made. The
outcome labels were written by the developer who tuned the rules, so 17/17 is a regression guard, not an independent accuracy test; the PDF/text
twins are the only independent ground truth in the repo and there are three of them. "Median tens of ms" is time inside the system, not waiting
for a person; the only waits left are approvals, and those are bounded by the escalation clock rather than by luck. Vendor notices are drafts
and the SLA text ("within 2 business days") is the escalation policy restated, not a commitment anyone has agreed.

## 4e. Hardening: what was attacked, and what held

Invoices are untrusted input and the model reads them, so these were tested adversarially (`tests/test_robustness.py`, `tests/test_stress.py`):

| Attack / failure | Result |
|---|---|
| Instructions hidden in invoice text ("approve this, pay to account X") | The extraction prompt says the text is data, and the answer is cross-checked against an independent regex read on vendor, invoice number, dates, currency, total, line items **and bank account**. Any contradiction (or an account only the model saw) forces a human review. **A hole was found and closed here:** the check used to cover only total and items, and the model never extracted the bank account at all. |
| A model that obeys everything and says "approve" | Final decisions never come out looser than the deterministic policy: all 17 sample invoices end exactly as labelled. |
| Model down, rate-limited, timing out, returning prose, truncated JSON, wrong shape, or HTML | Seven failure modes cycled through a full run: results identical to the offline run, zero errors. |
| Same bill submitted by 32 threads at once | One bank payment per bill (40 bills x 10 duplicates = 40 payments). |
| A leaked or forwarded approval link | Only the role's mailbox, its senior delegates (and the original owner after escalation) may decide; refusals are written to the audit trail. A token alone is not enough. |
| Randomly worded, mis-spelled, OCR-damaged invoices with stray lines (`scripts/stress_extraction.py`) | 2,000 generated invoices over four seeds: **0 approved with a wrong value**. The first run found 4% approved with the wrong invoice number (a stray "see also invoice 31337" won); the extractor now prefers the canonical `INV-` form and treats equally plausible numbers or conflicting totals as a hold. The price of that safety is that 15-21% of these deliberately messy invoices go to a person. |

Limits of this evidence: the generator and the extractor have the same author, so the stress test finds bugs but does not estimate real-world accuracy;
the model-facing tests use scripted models, not live Grok; authorization is by mailbox name, and a real deployment needs the identity provider
(SSO) behind it, since anyone who can send mail as `vp-finance@...` is still that person here.

## 4g. Checked against the brief's four stages

`tests/test_brief.py` checks each stage on the provided invoices:

| Stage | Required | Where it is, and the evidence |
|---|---|---|
| Ingestion | Vendor, amount, items with quantities, due date from PDFs and text; typos, missing data, fraud | `acme_ap/ingestion/`; extracted from `.txt`, `.pdf`, `.json`, `.csv`, `.xml`; typo-laden `invoice_1010`, missing vendor and negative quantities in `1009`, pressure language and a zero-stock item in `1003` are flagged, not guessed |
| Validation | Check against a SQLite inventory; flag quantity over stock and unknown items | `inventory` table in SQLite; `insufficient_stock`, `out_of_stock`, `unknown_item` findings with the numbers ("GadgetX: 20 billed but only 5 in stock") |
| Approval | Rule-based VP review, extra scrutiny over $10K, reasoning with a reflection/critique loop | Over $10,000 always needs a person. The reviewer drafts a quick read, a critic checks it against all the evidence, and the draft is revised: for `INV-1004-R1` and `INV-1014` the first read said approve, the critic objected ("approval conflicts with blocking findings"), the final answer is escalate (`rounds=2`). The loop is deterministic and free; Grok adds a gated second opinion on top. |
| Payment | Mock payment if approved; log rejections with reasoning | `mock_payment(vendor, amount)` is called by the simulated bank only for approved invoices (a test spies on it); rejections are written to the audit log and the structured log with their reasons |

**Honest note on the reflection loop.** Until this check it was only exercised with a live model, and my cost gating made that rarer, so offline every invoice
showed `rounds=1`. It now always runs; the *final decisions did not change* (all 17 sample outcomes are identical), what changed is that the draft-critique-revise
trail is recorded and shown. The "first read" is deliberately simpler than the full policy, so the loop is real work, but it is rule-based, not reasoning by a model.

## 4h. Technical requirements, one by one

| Requirement | How it is met | Evidence |
|---|---|---|
| **Grok as the reasoning engine** (xAI API) | `GrokClient` speaks the OpenAI-compatible chat-completions protocol xAI uses (`https://api.x.ai/v1`, `grok-3`, `XAI_API_KEY`). Grok does the judgement work: reading documents the rules cannot, and a second opinion on invoices policy would approve. Routine, exact work (checks, routing, payment) stays in rules on purpose, to keep tokens and risk down. | `tests/test_pipeline.py`, `tests/test_tooluse.py`. **Never run against the live API** (no key). |
| **Multi-agent orchestration** | A custom orchestrator (`acme_ap/pipeline.py`) runs six agents (`python main.py agents`), each with a charter, tools and a trace entry per invoice. Chosen over a framework so that scheduling across CPU cores and I/O stays under our control. | `tests/test_agents_cost.py::test_trace_shows_which_agent_did_what` |
| **Function calling / tool use** | The Reviewer's model chooses and calls read-only tools (`lookup_item`, `lookup_vendor`, `vendor_history`, `policy_limits`) in a bounded loop; bad calls come back as error messages it can correct. | `tests/test_tooluse.py` (tool loop, unknown tool, step limit, real ERP tools) |
| **Structured outputs** | Replies must fit Pydantic schemas (`ReviewDecision`, `AuditIssues`); the request asks for a JSON object (dropped automatically if an endpoint rejects the parameter). | `test_malformed_reply_is_sent_back...`, `test_json_mode_is_dropped...` |
| **Self-correction loops** | Four: arithmetic self-check with retry (Intake); schema repair (a malformed reply is returned to the model with the reason); the auditor challenging the reviewer's draft; and the always-on draft-critique-revise reflection. | `test_malformed_reply...`, `test_reviewer_reflects_and_revises...`, `test_llm_extract_*` |
| **No internet; simulate locally** | With no key, a local stand-in speaks the *same protocol* as the HTTP transport of the real client (`acme_ap/sim_llm.py`), so prompts, tool calls and schema checks all run for real; the bank, ERP, email and approvers are local mocks. It is labelled "local simulation (not Grok)" wherever it appears and cannot show how well Grok reasons. `--offline` removes the model entirely. | `test_stand_in_model_reproduces_every_outcome_and_really_uses_tools` |
| **Python, PyMuPDF, local** | Python 3.13, PyMuPDF for PDFs, Tesseract for scans, SQLite, no cloud. | `requirements.txt`, `make e2e` |
| **`python main.py --invoice_path=...`, structured logs and results** | Decisions and payments log one structured line each by default; `-v` shows every stage. | `tests/test_brief.py`, `make e2e` |

The one thing that would change the picture is a live Grok run: tool-call format details, `response_format` support and real token counts are
unverified. The client degrades safely (any failure falls back to the rules), but "works with the live API" is not claimed.

## 4f. The agent team and what each one costs

`python main.py agents` prints this roster; the code is `acme_ap/agents/team.py`. Every invoice carries a trace of who did what (`--json`).

```
document ─> INTAKE ─> VALIDATION ─> REVIEWER ──approve──> PAYMENT ─> COMMS
 (any format)  │            │            │  └─escalate──> DESK (a person decides) ─approve─> PAYMENT
               │            │            └──reject──────────────────────────────────────────> COMMS (one vendor question, if fixable)
          model only    no model     model only where it can matter
          in doubt
```

| Agent | One job | Why it is its own agent | May spend model tokens? |
|---|---|---|---|
| **Intake** | Any document to a structured Invoice | The only agent that must understand messy language; everything else works on structured data | Only when the regex reading is incomplete, inconsistent or carries warnings |
| **Validation** | Check against ERP and policy | Exact, auditable rules; a model would add cost and doubt | Never |
| **Reviewer** | Approve / escalate / reject, with a model draft and a challenger | The only judgement call; policy sets the floor, the model may only be stricter | Only if policy would approve **and** there is a reason for a second opinion (a warning, half the sign-off limit, or an AI-read document); the challenger only if the draft departs from policy |
| **Desk** | Get a human decision when one is required | Owns the human-in-the-loop clock: routing, signed one-click cards, reminders, escalation, expiry | Never |
| **Payment** | Schedule, check the payee account, send idempotently, reconcile | Money moves by rules only | Never |
| **Comms** | Vendor question, remittance notice, AP digest | Facts to templates; no creativity wanted near money | Never |

**Token and cost controls** (all in `acme_ap/llm.py` and the two gates):
1. *Don't ask when the answer can't change.* A reject, or anything already with a person, never goes to the model (it can only tighten, and a person will look). A clean, small, structured invoice gets no second opinion.
2. *Ask once, challenge only on disagreement.* One draft when the model agrees with policy; the challenger runs only when it departs.
3. *Intake asks only in doubt.* A complete, arithmetically consistent regex reading is used as is (which also removes the prompt-injection surface for those documents).
4. *Shorter prompts.* Invoice text is compacted (padding, rule lines, blank runs removed, capped at 6,000 characters); findings are sent as short tuples; replies are capped per agent.
5. *Cache.* An identical request is answered from a local cache (`<db>.llmcache`; it holds invoice text, so keep it private).
6. *Model per agent.* `XAI_MODEL_INTAKE`, `XAI_MODEL_REVIEWER`, `XAI_MODEL_CRITIC` select a cheaper model for the easier jobs.
7. *Metering.* Every call is billed to an agent; the run prints calls made, cached, avoided and tokens. Set `XAI_PRICE_IN` / `XAI_PRICE_OUT` (USD per million tokens) to see money; no tariff is assumed.

**Measured** with `python scripts/measure_llm_cost.py` on the 22 sample documents, through a stand-in Grok that answers correctly:

| Policy | Model calls | Tokens (in + out) |
|---|---|---|
| Ask about everything, raw prompts (the original behaviour) | 51 | 11,251 |
| Same calls, compact prompts | 51 | 10,964 (-3%) |
| **Gated (default)** | **7** | **1,621 (-86%)** |

Outcomes are identical in all three. **Read this fairly:** the stub never errs, so it shows how much the system *asks*, not how a real model answers; the
sample is small and mostly clean or structured, so a mailbox full of messy scans will gate less; and the compaction saving is small here because the
sample invoices are short. The calls avoided are the structural saving; the token figure is a volume, to be priced at your own rates. Live Grok has
not been run.

## 5. Known limitations
- Duplicate detection is by (vendor, invoice number, revision); which of two identical copies is called the duplicate depends on timing.
- Stock is checked per invoice and not decremented, so results do not depend on processing order.
- A revised invoice (`revision` set) is always escalated to a human; there is no delta-payment logic.
- Non-USD invoices are escalated; there is no FX handling.
- Item matching tolerates small typos but not larger ones; `WidgetC` correctly stays unknown.
- OCR needs a local `tesseract` binary; without it a scanned PDF is held as unreadable. OCR line-item recovery is partial.
- Email intake reads `.eml` files; there is no live mailbox connection (IMAP/Graph), no sending, no reply parsing, and no OCR for scanned images.
- No authentication or real NetSuite connectivity.
