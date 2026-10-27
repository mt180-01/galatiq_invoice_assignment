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
| 2,000 | 1,192 docs/s | 1,643 docs/s | 1.4x | 90% (mostly worker start-up) |
| 20,000 | 1,240 docs/s | 3,142 docs/s | 2.5x | 41% |

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

## 5. Known limitations
- Duplicate detection is by (vendor, invoice number, revision); which of two identical copies is called the duplicate depends on timing.
- Stock is checked per invoice and not decremented, so results do not depend on processing order.
- A revised invoice (`revision` set) is always escalated to a human; there is no delta-payment logic.
- Non-USD invoices are escalated; there is no FX handling.
- Item matching tolerates small typos but not larger ones; `WidgetC` correctly stays unknown.
- OCR needs a local `tesseract` binary; without it a scanned PDF is held as unreadable. OCR line-item recovery is partial.
- Email intake reads `.eml` files; there is no live mailbox connection (IMAP/Graph), no sending, no reply parsing, and no OCR for scanned images.
- No authentication or real NetSuite connectivity.
