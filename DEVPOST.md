# AgentPay: talk to your PayPal

*Draft Devpost description. Edit the bracketed parts before submitting.*

**Tagline:** An AI agent that runs invoices, checkout links, refunds and payouts on PayPal from plain-language requests, and always asks before money moves.

## Inspiration
Freelancers and small online sellers, in India selling to clients abroad and elsewhere, do their own billing. That means opening a dashboard, filling the same invoice form, making a payment link for a WhatsApp customer, hunting down which order to refund, and scrolling to see who still owes money. Each task is small, but they pile up and interrupt real work. We wanted the seller to just *say* the task, and have an agent do the PayPal part without ever moving money on its own.

## What it does
You type, or dictate with the mic button, things like:
- "Invoice Priya $40 for logo design, due Friday": drafts a PayPal invoice with the due date resolved, then shows a confirmation card before sending.
- "Make a checkout link for 2 T-shirts at $15": creates a PayPal order with line items and returns the buyer's checkout link. When the buyer pays and returns, the payment is captured.
- "Refund $5 of order …" / "Refund the last order": checks the refundable balance and proposes a partial or full refund for approval.
- "Who hasn't paid?": lists open invoices and unpaid links, totals what you're owed per currency, flags overdue ones, and offers "Remind everyone".
- "Cancel Arjun's invoice", "Pay $20 to alex@… and $10 to sam@…" (Payouts for splitting with collaborators).

A live ledger beside the chat shows invoices, orders, actions awaiting approval, and an activity log.

**Human-in-the-loop by design:** sending invoices, reminders, cancellations, refunds and payouts all become a confirmation card. They only run when the seller clicks Approve on a single-use, server-held action ID.

## How we built it
- **Backend:** Python + FastAPI. A small PayPal REST client built on httpx: OAuth2 client-credentials with token caching, idempotency keys, and calls to Invoicing v2, Orders v2, Payments v2 (refunds) and Payouts v1.
- **AI:** LLM function calling (OpenAI or Gemini, chosen by env var). The tools are JSON-schema definitions. The model gets today's date and recent context and returns tool calls. The server validates every argument (amount cap, email, currency, quantity), resolves "Priya" or "the last order" to real PayPal IDs, and gates money-moving tools behind the confirmation step.
- **Mock mode:** with no keys, an in-memory PayPal fake with the same lifecycles and rules, plus a deterministic intent parser in place of the LLM. Judges can run the full loop, including simulated buyer checkout pages, with zero credentials.
- **Frontend:** one HTML/JS file with no framework: chat, confirmation cards, ledger polling, and browser speech recognition for voice.
- **Tests:** pytest suites for the intent parser, the PayPal client (mocked HTTP), the LLM adapters (mocked HTTP) and end-to-end API flows in mock mode.

## Challenges we ran into
- **Deciding where the agent may act alone.** We settled on: drafts, links and read-only summaries run immediately; anything that sends money or messages a customer waits for a click. The LLM can't confirm its own action because confirmation IDs live only on the server.
- **Turning loose references into exact objects.** "Refund the last order" and "remind Priya" have to map to one specific capture or invoice, with sensible behaviour when nothing matches, when an order is unpaid, or when a refund would exceed what's left.
- **PayPal response shapes.** Invoice creation can return a full object or just a link, and checkout links appear as `payer-action` or `approve` depending on the request style. We normalize everything into one small shape that the agent and UI share.
- **Making it demoable without keys** while keeping the fake honest: same statuses, same refusal rules, and pages clearly marked as simulated.

## Accomplishments that we're proud of
- A full seller loop (invoice → send → remind/cancel, checkout → capture → partial/full refund, payouts, unpaid summary) driven by plain language.
- Human approval enforced on the server, not just shown in the UI.
- Runs with zero credentials for judges, and switches to the real PayPal sandbox and a real LLM through environment variables only.
- An automated test suite covering the parser, the PayPal client, the LLM adapters and the end-to-end flows.

## What we learned
- Agentic commerce works best when the agent does the tedious part (understanding, filling in, finding the right ID) and the human keeps the decision.
- Function calling plus strict server-side validation is a cleaner safety boundary than prompting the model to "be careful".
- [Add anything you learned from running it against the live sandbox.]

## What's next for AgentPay
- Persist ledger state in a database and add seller sign-in.
- Scheduled "chase" runs: draft reminders for overdue invoices each morning for one-tap approval.
- Invoice-payment refunds, recurring invoices, and PayPal webhooks instead of polling.
- WhatsApp / voice-note intake, so a seller can forward a customer message and get a ready-to-approve invoice.
- Multi-language commands (Hindi, Kannada and others).

## Built with
python · fastapi · httpx · pydantic · uvicorn · paypal-rest-api (invoicing-v2, orders-v2, payments-v2, payouts) · openai · gemini · javascript · html · css · pytest · docker

## Try it
- Repo: [add link after you publish]
- Run locally in mock mode: `pip install -r requirements.txt && uvicorn app.main:app` → http://localhost:8000
- Demo video: [add link]
