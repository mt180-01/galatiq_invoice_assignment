# Recorded live runs

Real runs against the xAI Grok API (not the local stand-in), saved so the claim "it works with live Grok" can be checked.

| File | What it is |
|---|---|
| `2026-10-27-provided-invoices.txt` | The terminal output of `python main.py --grok --trace --invoice_path data/invoices`: structured log lines, results table, impact, AI usage with the **cost xAI reported**, and the per-invoice agent trace (model calls, tool calls, conclusions). |
| `2026-10-27-provided-invoices.json` | The same run's full results (`--json`): every invoice's findings, decision, agent trace with model/tool events, the model-written notes and follow-ups, and token usage and cost per invoice. |

Result: 17 of 17 outcomes as labelled, 0 errors, 28 model calls, 4 tool calls, **$0.0637** as reported by xAI. Native `json_schema` structured output was accepted by the API.

Honest notes: the 28 calls include extraction, a second opinion before each of the 7 payments, the item/vendor resolver, and the plain-English notes and follow-ups.
Grok made few tool calls on these invoices because the checks it was given were already sufficient; tool calling itself is exercised in `scripts/live_smoke.py`
and in `data/self_correction/`. Temporary folders were scrubbed from the text and the JSON; no credential is in either file (`tests/test_no_secrets.py` scans the repo).
Reproduce with your own key: `python main.py --grok --trace --invoice_path data/invoices` (about six cents).
