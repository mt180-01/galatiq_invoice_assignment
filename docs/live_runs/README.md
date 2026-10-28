# Recorded live runs

Real runs against the xAI Grok API (not the local stand-in), saved so the claim "it works with live Grok" can be checked, and **replayed offline for free**.

| File | What it is |
|---|---|
| `provided-invoices.cassette.jsonl` | The recording: every request/response exchange of the run (36, all successful), keyed by a hash of the request. No headers, so no key. This is what `--replay-llm` reads. |
| `2026-10-27-provided-invoices.txt` | The terminal output of that run (`--trace`): structured log lines, results, impact, AI usage with the **cost xAI reported**, and the per-invoice agent trace (model calls, tool calls, conclusions). |
| `2026-10-27-provided-invoices.json` | The same run's full results (`--json`): findings, decisions, agent traces with model/tool events, the model-written notes and emails, tokens and cost per invoice. |

**Replay it** (no network, no key, no cost): `python main.py --replay-llm docs/live_runs/provided-invoices.cassette.jsonl --invoice_path data/invoices`. It reproduces the run:
17 of 17 outcomes, the same notes and emails, 36 model calls answered from the recording, **$0.0208** (what xAI charged when it was recorded). Per invoice, with the rules-only run
alongside, see [`../EVALUATION.md`](../EVALUATION.md). Record again with `--record-llm FILE` (needs a key; about two cents). Recording and replay process documents one at a time in file order (`--sequential`, implied), so which copy of a duplicated invoice is blocked cannot vary with thread timing: an earlier recording made with concurrent processing replayed differently from run to run, and the evaluation document was flaky until this was fixed.

What the run shows: 17/17 outcomes as labelled and 0 errors; **36 model calls and 42 tool calls**, all served by `grok-4.20-0309-non-reasoning`; slowest step 5.6 seconds; $0.0208 for the
17 invoices (about $0.0012 each). Native `json_schema` structured output was accepted by the API. The model changed no decision (it can only tighten one, and these invoices are clear-cut);
it gave a second opinion before each of the 7 payments, wrote 10 plain-English notes and 4 vendor emails, and searched for name fixes on 2 invoices that stayed rejected.

## What earlier recordings taught us (all fixed, each with a regression test)

* **`grok-3` was served by a different, slower, dearer model.** On the account used, asking for `grok-3` returned `grok-4.3`, a reasoning model: ~5 s and three times the cost per
  small call, against 1-2 s for the plain model. Calls now use a non-reasoning model when you have not named one (`XAI_MODEL` is always respected).
* **A race in that selection** let the first few concurrent calls go out under the old name; found in the recording (five exchanges served by the slow model), fixed with a lock.
* **Optional wording must not stall an invoice.** A courtesy email once held an invoice up for 94 seconds (three 30-second timeouts plus backoff). Notes and emails now get a 12-second
  limit and no retries; on timeout the template is used. A run with the slow model recorded 12 such timeouts in 29 requests, and the system still got all 17 outcomes right.
* **Failures are part of the record.** The recorder keeps 429s and timeouts, so a replay goes through the same retries and fallbacks as the live run did.

Temporary folders were scrubbed from the text and JSON; no credential is in any file (`tests/test_no_secrets.py` scans the repo, and a test checks the recording for keys and headers).
