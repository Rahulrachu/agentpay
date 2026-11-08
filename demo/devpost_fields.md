# Devpost fields (ready to paste)

## Project name
AgentPay

## Elevator pitch / tagline (≤200 chars)
An AI agent that runs PayPal invoices, checkout links, reminders and payouts from plain-language requests, and always asks before money or messages move.

## Inspiration
Freelancers and small online sellers do their own billing. That means opening a dashboard, filling the same invoice form, making a payment link for a WhatsApp customer, and scrolling to see who still owes money. Each task is small, but they pile up and interrupt real work. We wanted the seller to just say the task, and have an agent do the PayPal part without ever moving money on its own.

## What it does
You type, or dictate with the mic button, what you need:
- "Bill Rohan (rohan@…) $75 for a website audit, due end of next week" drafts a PayPal invoice with the due date worked out, then shows an approval card before sending.
- "Invoice Meera $60 and Kabir $35 for the workshop" becomes a multi-step plan with one approval per send, or "Approve all".
- "Create a checkout link for $20 for a consultation" creates a PayPal order and returns the buyer's checkout link.
- "Who hasn't paid me?" lists open invoices and unpaid links, totals what you're owed, flags overdue ones, and offers "Remind everyone unpaid".
- "Pay $15 to alex@… for design help" sends a PayPal payout to a collaborator, after approval.
- It can also cancel invoices and refund checkout orders.

A live ledger shows invoices, orders and actions awaiting approval, and an audit log records every proposal, approval, decline and execution. Sending invoices, reminders, cancellations, refunds and payouts only run when the seller clicks Approve on a single-use, server-held action ID.

## How we built it
- Python + FastAPI backend with SQLite for the ledger, plans and audit log.
- A small PayPal REST client on httpx: OAuth2 client credentials with token caching, idempotency keys, and PayPal Invoicing v2, Orders v2, Payments v2 (refunds) and Payouts v1.
- LLM function calling with Gemini (gemini-3.5-flash-lite on the live demo; OpenAI also supported). Tools are JSON-schema definitions; the model proposes tool calls, and the server validates every argument (amount cap, email, currency, quantity), resolves names like "Rohan" to real PayPal IDs, and puts money-moving tools behind approval.
- A mock mode (in-memory PayPal fake plus a rule-based parser) so anyone can run the full loop with no keys.
- One HTML/JS page with no framework: chat, approval and plan cards, ledger, audit log, and browser speech recognition.
- Deployed on Render; pytest suites cover the parser, PayPal client, LLM adapters and end-to-end flows.

## Challenges we ran into
- Deciding where the agent may act alone: drafts, links and read-only summaries run immediately; anything that sends money or messages a customer waits for a click, and the model can't approve its own actions because approval IDs live only on the server.
- Turning loose references ("remind Rohan", "the last order") into one exact PayPal object, with sensible behaviour when nothing matches.
- Normalizing PayPal response shapes (full objects vs. links, `payer-action` vs. `approve` links) into one shape for the agent and the UI.
- Country rules: our first sandbox business account was India-registered, and PayPal blocks India-registered merchants from sending payouts and from invoicing in INR. We moved the live demo to a US sandbox merchant app, where payouts work, and the agent explains those PayPal errors in plain words.

## Accomplishments that we're proud of
- A plain-language seller loop running on the real PayPal sandbox: invoice and send, multi-recipient plans, checkout links, the unpaid summary, reminders and payouts.
- Human approval enforced on the server, not just shown in the UI, with every step in an audit log.
- Zero-credential mock mode, and a switch to the real sandbox and a real LLM through environment variables only.
- An automated test suite (102 tests passing).

## What we learned
- Agentic commerce works best when the agent does the tedious part (understanding, filling in, finding the right ID) and the human keeps the decision.
- Function calling plus strict server-side validation is a cleaner safety boundary than prompting the model to "be careful".
- The live sandbox surfaced what mocks can't: which PayPal features depend on the merchant's country (payouts verified on a US sandbox merchant app; India-registered merchants are blocked for payouts and INR invoices), and that invoices without a customer email can't be reminded.

## What's next for AgentPay
- Seller sign-in and per-seller ledgers.
- Scheduled "chase" runs that draft reminders for overdue invoices each morning for one-tap approval.
- Invoice-payment refunds, recurring invoices, and PayPal webhooks instead of polling.
- WhatsApp / voice-note intake: forward a customer message, get a ready-to-approve invoice.
- Commands in Hindi, Kannada and other languages.

## Built with
python, fastapi, httpx, pydantic, uvicorn, sqlite, paypal-invoicing-api, paypal-orders-api, paypal-payouts-api, paypal-payments-api, gemini, openai, javascript, html, css, pytest, docker, render

## "Try it out" links
- https://agentpay-4lga.onrender.com
- https://github.com/Rahulrachu/agentpay

## Video demo link
[paste the unlisted YouTube URL after uploading demo/agentpay_demo.mp4]

## Note for judges (optional field)
Live demo runs on the PayPal sandbox with demo addresses only. Verified live: invoice create/send/list/remind/cancel, multi-recipient plans, checkout links, unpaid summary, and payouts (US sandbox merchant app). Capture after a buyer pays and refunds are implemented and covered by mocked-HTTP tests and mock mode, but not yet run live. The free Render tier can take about a minute to wake up.
