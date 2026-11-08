# AgentPay: talk to your PayPal

**Tagline:** An AI agent that runs PayPal invoices, checkout links, reminders and payouts from plain-language requests, and always asks before money or messages move.

- **Live demo:** https://agentpay-4lga.onrender.com (PayPal sandbox + Gemini; free tier, so the first load can take about a minute to wake up)
- **Code:** https://github.com/Rahulrachu/agentpay

## Inspiration
Freelancers and small online sellers, in India selling to clients abroad and elsewhere, do their own billing. That means opening a dashboard, filling the same invoice form, making a payment link for a WhatsApp customer, hunting down which order to refund, and scrolling to see who still owes money. Each task is small, but they pile up and interrupt real work. We wanted the seller to just *say* the task, and have an agent do the PayPal part without ever moving money on its own.

## What it does
You type, or dictate with the mic button, things like:
- "Bill Rohan (rohan@…) $75 for a website audit, due end of next week": drafts a PayPal invoice with the due date resolved, then shows an approval card before sending.
- "Invoice Meera $60 and Kabir $35 for the workshop": becomes a multi-step plan with one approval per send, or "Approve all".
- "Create a checkout link for $20 for a consultation": creates a PayPal order and returns the buyer's checkout link. When the buyer pays and returns, the payment is captured.
- "Who hasn't paid me?": lists open invoices and unpaid links, totals what you're owed per currency, flags overdue ones, and offers "Remind everyone unpaid".
- "Pay $15 to alex@… for design help": a PayPal payout to a collaborator, after approval.
- "Cancel Kabir's invoice", "Refund the last order".

A live ledger beside the chat shows invoices, orders, actions awaiting approval, and an audit log of every proposal, approval, decline and execution.

**Human-in-the-loop by design:** sending invoices, reminders, cancellations, refunds and payouts all become an approval card. They only run when the seller clicks Approve on a single-use, server-held action ID.

## How we built it
- **Backend:** Python + FastAPI, SQLite for the ledger, plans and audit log. A small PayPal REST client built on httpx: OAuth2 client-credentials with token caching, idempotency keys, and calls to Invoicing v2, Orders v2, Payments v2 (refunds) and Payouts v1.
- **AI:** LLM function calling (Gemini `gemini-3.5-flash-lite` on the live demo; OpenAI also supported, chosen by env var). The tools are JSON-schema definitions. The model gets today's date and recent context and returns tool calls. The server validates every argument (amount cap, email, currency, quantity), resolves "Rohan" or "the last order" to real PayPal IDs, and gates money-moving tools behind the approval step.
- **Mock mode:** with no keys, an in-memory PayPal fake with the same lifecycles and rules, plus a deterministic intent parser in place of the LLM. Anyone can run the full loop, including simulated buyer checkout pages, with zero credentials.
- **Frontend:** one HTML/JS file with no framework: chat, approval cards, plan cards, ledger, audit log, and browser speech recognition for voice.
- **Deploy:** Render web service, redeployed on every push to GitHub.
- **Tests:** pytest suites for the intent parser, the PayPal client (mocked HTTP), the LLM adapters (mocked HTTP) and end-to-end API flows in mock mode.

## Challenges we ran into
- **Deciding where the agent may act alone.** We settled on: drafts, links and read-only summaries run immediately; anything that sends money or messages a customer waits for a click. The LLM can't approve its own action because approval IDs live only on the server.
- **Turning loose references into exact objects.** "Refund the last order" and "remind Rohan" have to map to one specific capture or invoice, with sensible behaviour when nothing matches, when an order is unpaid, or when a refund would exceed what's left.
- **PayPal response shapes.** Invoice creation can return a full object or just a link, and checkout links appear as `payer-action` or `approve` depending on the request style. We normalize everything into one small shape that the agent and UI share.
- **Country rules on the sandbox.** Our first sandbox business account was India-registered, and PayPal blocks India-registered merchants from sending payouts and from invoicing in INR. We moved the live demo to a US sandbox merchant app, where payouts work, and the agent explains those PayPal errors in plain words instead of failing silently.
- **Making it demoable without keys** while keeping the fake honest: same statuses, same refusal rules, and pages clearly marked as simulated.

## Accomplishments that we're proud of
- A seller loop driven by plain language and run against the real PayPal sandbox: invoice → approve → send, multi-recipient plans, checkout links, the unpaid summary, reminders and payouts.
- Human approval enforced on the server, not just shown in the UI, with every step in an audit log.
- Runs with zero credentials in mock mode, and switches to the real PayPal sandbox and a real LLM through environment variables only.
- An automated test suite covering the parser, the PayPal client, the LLM adapters and the end-to-end flows.

## What we learned
- Agentic commerce works best when the agent does the tedious part (understanding, filling in, finding the right ID) and the human keeps the decision.
- Function calling plus strict server-side validation is a cleaner safety boundary than prompting the model to "be careful".
- Running against the live sandbox surfaced things mocks never would: which PayPal features depend on the merchant's country (payouts were verified on a US sandbox merchant app; India-registered merchants are blocked by PayPal for payouts and INR invoices), that invoices without a customer email can't be reminded, and that a small, fast model is enough for reliable tool calling when the server does the validation.

## What we verified on the live sandbox
- Verified live: Invoicing v2 create, send, list, remind and cancel; multi-recipient invoice plans; Orders v2 checkout links; the unpaid summary; Payouts v1 batch submission (on the US sandbox merchant app).
- Implemented and covered by mocked-HTTP tests and mock mode, not yet run live: capture after a buyer pays a checkout link, and Payments v2 refunds (these need a sandbox buyer to complete a payment first).

## What's next for AgentPay
- Seller sign-in and per-seller ledgers.
- Scheduled "chase" runs: draft reminders for overdue invoices each morning for one-tap approval.
- Invoice-payment refunds, recurring invoices, and PayPal webhooks instead of polling.
- WhatsApp / voice-note intake, so a seller can forward a customer message and get a ready-to-approve invoice.
- Multi-language commands (Hindi, Kannada and others).

## Built with
python · fastapi · httpx · pydantic · uvicorn · sqlite · paypal-rest-api (invoicing-v2, orders-v2, payments-v2, payouts) · gemini · openai · javascript · html · css · pytest · docker · render

## Try it
- Live: https://agentpay-4lga.onrender.com
- Repo: https://github.com/Rahulrachu/agentpay
- Run locally in mock mode: `pip install -r requirements.txt && uvicorn app.main:app` → http://localhost:8000
