"""The agent's tool catalogue. The same tool names and argument shapes are produced by the
LLM (via function calling) and by the deterministic intent parser, so the executor in
agent.py does not care which "brain" picked the action.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Actions that move money or message a customer. These are NEVER executed directly:
# the agent turns them into a confirmation card and waits for the seller's click.
CONFIRM_TOOLS = {"send_invoice", "remind_invoice", "cancel_invoice", "refund_order", "create_payout"}


@dataclass
class ToolCall:
    name: str
    args: dict = field(default_factory=dict)


@dataclass
class Plan:
    tool_calls: list[ToolCall] = field(default_factory=list)
    reply: str = ""  # free text from the brain (used when no tool fits)


_ref = {"type": "string", "description": "Invoice ID, invoice number, or the customer's name"}
_order_ref = {"type": "string",
              "description": "Order ID, 'LAST' for the most recent paid order, or the buyer's name / an item name"}

TOOL_SCHEMAS: list[dict] = [
    {
        "name": "create_invoice",
        "description": "Create a PayPal draft invoice for a customer. The seller is then asked to confirm sending it.",
        "parameters": {
            "type": "object",
            "properties": {
                "recipient_name": {"type": "string"},
                "recipient_email": {"type": "string", "description": "Customer email if the seller gave one"},
                "amount": {"type": "number"},
                "currency": {"type": "string", "description": "ISO code like USD, INR, EUR"},
                "description": {"type": "string", "description": "What the invoice is for"},
                "due_date": {"type": "string", "description": "YYYY-MM-DD, resolved from phrases like 'Friday'"},
            },
            "required": ["recipient_name", "amount", "description"],
        },
    },
    {
        "name": "send_invoice",
        "description": "Send an existing draft invoice to the customer (requires seller confirmation).",
        "parameters": {"type": "object", "properties": {"invoice_ref": _ref}, "required": ["invoice_ref"]},
    },
    {
        "name": "list_invoices",
        "description": "List recent invoices, optionally filtered by status (DRAFT, SENT, PAID, CANCELLED).",
        "parameters": {"type": "object", "properties": {"status": {"type": "string"}}},
    },
    {
        "name": "unpaid_summary",
        "description": "Summarize who has not paid: open invoices (with overdue flags) and unpaid checkout links.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "remind_invoice",
        "description": ("Send a payment reminder for an unpaid invoice. Use invoice_ref 'ALL_UNPAID' for everyone "
                        "who owes money, or 'ALL_OVERDUE' for only invoices past their due date."),
        "parameters": {"type": "object", "properties": {"invoice_ref": _ref, "note": {"type": "string"}},
                       "required": ["invoice_ref"]},
    },
    {
        "name": "cancel_invoice",
        "description": "Cancel a sent, unpaid invoice (requires seller confirmation).",
        "parameters": {"type": "object", "properties": {"invoice_ref": _ref}, "required": ["invoice_ref"]},
    },
    {
        "name": "create_checkout_link",
        "description": "Create a PayPal checkout order for one or more items and return the buyer's payment link.",
        "parameters": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "quantity": {"type": "integer"},
                            "unit_amount": {"type": "number", "description": "Price of one unit"},
                        },
                        "required": ["name", "quantity", "unit_amount"],
                    },
                },
                "currency": {"type": "string"},
            },
            "required": ["items"],
        },
    },
    {
        "name": "capture_order",
        "description": "Capture payment for a checkout order the buyer has approved.",
        "parameters": {"type": "object", "properties": {"order_ref": _order_ref}, "required": ["order_ref"]},
    },
    {
        "name": "refund_order",
        "description": "Refund a paid order, fully or partially (requires seller confirmation).",
        "parameters": {"type": "object",
                       "properties": {"order_ref": _order_ref,
                                      "amount": {"type": "number", "description": "Omit for a full refund"}},
                       "required": ["order_ref"]},
    },
    {
        "name": "create_payout",
        "description": "Pay collaborators by email via PayPal Payouts, e.g. to split a payment (requires confirmation).",
        "parameters": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"receiver_email": {"type": "string"}, "amount": {"type": "number"},
                                       "note": {"type": "string"}},
                        "required": ["receiver_email", "amount"],
                    },
                },
                "currency": {"type": "string"},
            },
            "required": ["items"],
        },
    },
]

REASON_PROPERTY = {
    "type": "string",
    "description": "One short sentence, shown to the seller, on why this action fits their request.",
}
for _t in TOOL_SCHEMAS:  # every tool accepts an optional explanation; agent.py pops it before validation
    _t["parameters"].setdefault("properties", {})["reason"] = REASON_PROPERTY

TOOL_NAMES = {t["name"] for t in TOOL_SCHEMAS}
