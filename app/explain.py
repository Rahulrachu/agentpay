"""One-line explanations of why the agent chose an action, shown to the seller on every step.

With an LLM brain the model's own `reason` argument is used (trimmed and length-capped).
Without one (or when the model leaves it out) a deterministic sentence is built from the tool
and its arguments, so the explanation never claims more than the code actually checks.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

SYMBOL = {"USD": "$", "INR": "₹", "EUR": "€", "GBP": "£"}


def _money(value, currency) -> str:
    try:
        d = Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return str(value)
    cur = (currency or "").upper()
    return f"{SYMBOL[cur]}{d:,}" if cur in SYMBOL else f"{cur} {d:,}".strip()


def explain(tool: str, args: dict | None = None, reason: str = "") -> str:
    reason = " ".join(str(reason or "").split())
    if reason:
        return reason[:220]
    a = args or {}
    if tool == "create_invoice":
        who = a.get("recipient_name") or "the customer"
        amt = _money(a.get("amount", "?"), a.get("currency") or "USD")
        return (f"You asked to bill {who} {amt}. I start with a PayPal draft so nothing reaches "
                f"{who} until you approve sending it.")
    if tool == "send_invoice":
        return "Sending an invoice contacts the customer, so it waits for your approval."
    if tool == "list_invoices":
        return "A read-only lookup of your PayPal invoices; nothing is changed."
    if tool == "unpaid_summary":
        return "You asked who owes you. This only reads open invoices and unpaid checkout links."
    if tool == "remind_invoice":
        ref = str(a.get("invoice_ref") or "").upper()
        if ref == "ALL_OVERDUE":
            return "You asked to chase late payers, so I picked only sent invoices past their due date."
        if ref == "ALL_UNPAID":
            return "You asked to remind everyone who owes you, so I picked every sent, unpaid invoice."
        return "A reminder emails the customer, so I matched the open invoice and wait for your approval."
    if tool == "cancel_invoice":
        return "Cancelling notifies the customer and can't be undone, so it waits for your approval."
    if tool == "create_checkout_link":
        return ("A checkout link only lets the buyer pay you after they approve on PayPal, "
                "so it's safe to create right away.")
    if tool == "capture_order":
        return "The buyer already approved this order on PayPal, so capturing completes the payment they agreed to."
    if tool == "refund_order":
        if a.get("amount") not in (None, ""):
            return "Money leaves your account, so I checked the refundable balance and wait for your approval."
        return "A full refund of what's left on the order. Money leaves your account, so it waits for you."
    if tool == "create_payout":
        return "Payouts send money from your balance, so each batch waits for your approval."
    return ""
