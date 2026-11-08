# AgentPay: 3-minute demo video script

**Setup before recording:** start the server fresh so the ledger is empty (`uvicorn app.main:app --port 8000`). Use sandbox keys + an LLM key if they're working; otherwise use mock mode and say so on screen. Browser at 1280×800, zoom 110%. Keep a second tab ready for the checkout page.

| Time | On screen | Voice-over |
|---|---|---|
| **0:00–0:20** | Title card → the AgentPay UI, empty ledger. Point at the mode badge in the header. | "Small sellers and freelancers juggle invoices, payment links, refunds and chasing late payers, usually between other work. AgentPay lets you just say what you want, and an AI agent does it on PayPal. You stay in control of anything that moves money." |
| **0:20–0:55** | Type (or click the mic and say): *Invoice Priya $40 for logo design, due Friday*. Show the draft message and the **confirmation card**. Hover over the "Needs your approval" line. Click **Approve**. Ledger shows Priya: DRAFT → SENT, with a pay link. | "The agent understood who, how much, what for, and turned 'Friday' into a real due date. It created a draft invoice on PayPal right away, but sending it reaches a customer, so it stops and asks me first. One click and it's sent. There was no email, so PayPal gives me a link I can share on WhatsApp." |
| **0:55–1:35** | Type *Make a checkout link for 2 T-shirts at $15*. Click the link → PayPal sandbox checkout (or the simulated page in mock mode) → pay. Return to the app: the order is COMPLETED and "Payment captured" appears in the chat. | "For product sales it creates a PayPal checkout order with the line items and gives me the buyer link. The buyer approves on PayPal, comes back, and the payment is captured automatically, because the buyer has already said yes." |
| **1:35–2:05** | Type *Refund $5 of order …* (paste ID from the ledger). The card shows partial refund, order total and capture ID, in an amber "danger" style. Approve. Then *Refund the last order*: the card offers the remaining $25. Approve. Ledger shows REFUNDED. | "Refunds are money leaving my account, so every one needs my approval. The agent knows what's refundable: it won't refund an unpaid order or go over what was captured." |
| **2:05–2:40** | Have a second invoice ready (Arjun, ₹2,500, already overdue). Type *Who hasn't paid?*. The table shows Priya "due in Nd" and Arjun "OVERDUE". Click the **Remind everyone unpaid** chip. One card lists both. Approve. | "'Who hasn't paid?' pulls my open invoices from PayPal, adds up what I'm owed per currency, flags what's overdue, and offers to chase them. Reminders email my customers, so again: one card, one click." |
| **2:40–3:00** | Quick flash of the architecture diagram from the README, then back to the UI. | "Under the hood: FastAPI, LLM tool-calling with OpenAI or Gemini, and PayPal Invoicing, Orders, Payments and Payouts APIs. The model only proposes actions; the server validates them and waits for the human. And with no keys at all it runs in a mock mode, so anyone can try it. AgentPay: talk to your PayPal." |

**Recording tips**
- If you record in mock mode, say "mock mode" once out loud. Don't present simulated pages as real PayPal.
- Keep each typed command on screen for a beat before pressing Enter.
- Don't show `.env`, API keys, or the PayPal developer dashboard credentials page.
