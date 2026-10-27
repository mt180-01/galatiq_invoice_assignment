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

## 5. Known limitations
- Duplicate detection is by (vendor, invoice number, revision); which of two identical copies is called the duplicate depends on timing.
- Stock is checked per invoice and not decremented, so results do not depend on processing order.
- A revised invoice (`revision` set) is always escalated to a human; there is no delta-payment logic.
- Non-USD invoices are escalated; there is no FX handling.
- Item matching tolerates small typos but not larger ones; `WidgetC` correctly stays unknown.
- Email intake reads `.eml` files; there is no live mailbox connection (IMAP/Graph), no sending, no reply parsing, and no OCR for scanned images.\n- No authentication or real NetSuite connectivity.
