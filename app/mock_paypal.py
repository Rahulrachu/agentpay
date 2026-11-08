"""In-memory stand-in for the PayPal REST API, used when no sandbox keys are configured.

It implements the same methods and normalized return shapes as PayPalClient, follows the
same status lifecycles (DRAFT -> SENT -> PAID / CANCELLED, CREATED -> APPROVED -> COMPLETED),
and enforces the same basic rules (e.g. you cannot refund more than was captured).
Checkout and invoice "pay" links point at local simulated pages under /mock/.
"""
from __future__ import annotations

from decimal import Decimal

from .paypal_client import PayPalError, money

FAKE_BUYER = {"name": {"given_name": "Sandbox", "surname": "Buyer"}, "email_address": "buyer@example.com"}


class MockPayPalClient:
    is_mock = True

    KINDS = ("invoices", "orders", "refunds", "payouts")

    def __init__(self, store=None):
        """`store` (app.store.Store, optional) makes the fake survive restarts like real PayPal would."""
        self.store = store
        self._n = 0
        self.invoices: dict[str, dict] = {}
        self.orders: dict[str, dict] = {}
        self.refunds: dict[str, dict] = {}
        self.payouts: dict[str, dict] = {}
        self.events: list[str] = []  # simple audit trail useful in tests
        if store is not None:
            self._n = int(store.get_meta("mock_counter", 0))
            for kind in self.KINDS:
                key = "batch_id" if kind == "payouts" else "id"
                getattr(self, kind).update({o[key]: o for o in store.all(f"mock_{kind}")})

    def _next(self, prefix: str) -> str:
        self._n += 1
        return f"{prefix}{self._n:04d}"

    def _commit(self) -> None:
        if self.store is None:
            return
        self.store.set_meta("mock_counter", self._n)
        for kind in self.KINDS:
            self.store.sync(f"mock_{kind}", getattr(self, kind))

    # -- Invoicing
    def create_draft_invoice(self, recipient_name, amount, currency, description,
                             recipient_email="", due_date="", note=""):
        inv_id = self._next("INV2-MOCK-")
        inv = {
            "id": inv_id, "number": f"{len(self.invoices) + 1:04d}", "status": "DRAFT",
            "recipient_name": recipient_name, "recipient_email": recipient_email,
            "amount": money(amount, currency)["value"], "currency": currency.upper(),
            "due_amount": money(amount, currency)["value"], "due_date": due_date,
            "description": description, "pay_url": "", "note": note, "reminders": 0,
        }
        self.invoices[inv_id] = inv
        self.events.append(f"create_invoice {inv_id}")
        self._commit()
        return dict(inv)

    def _inv(self, invoice_id: str) -> dict:
        if invoice_id not in self.invoices:
            raise PayPalError(f"Invoice {invoice_id} not found", 404)
        return self.invoices[invoice_id]

    def get_invoice(self, invoice_id):
        return dict(self._inv(invoice_id))

    def send_invoice(self, invoice_id, notify_recipient=True):
        inv = self._inv(invoice_id)
        if inv["status"] != "DRAFT":
            raise PayPalError(f"Invoice {invoice_id} is {inv['status']}, only drafts can be sent", 422)
        inv["status"] = "SENT"
        inv["pay_url"] = f"/mock/invoice/{invoice_id}"
        self.events.append(f"send_invoice {invoice_id}")
        self._commit()
        return dict(inv)

    def list_invoices(self, page_size=50):
        return [dict(i) for i in list(self.invoices.values())[-page_size:]]

    def remind_invoice(self, invoice_id, note=""):
        inv = self._inv(invoice_id)
        if inv["status"] not in {"SENT", "UNPAID", "PARTIALLY_PAID"}:
            raise PayPalError(f"Invoice {invoice_id} is {inv['status']}; reminders need a sent, unpaid invoice", 422)
        inv["reminders"] += 1
        self.events.append(f"remind_invoice {invoice_id}")
        self._commit()
        return dict(inv)

    def cancel_invoice(self, invoice_id, note=""):
        inv = self._inv(invoice_id)
        if inv["status"] not in {"SENT", "UNPAID", "PARTIALLY_PAID"}:
            raise PayPalError(f"Invoice {invoice_id} is {inv['status']}; only sent, unpaid invoices can be cancelled", 422)
        inv["status"] = "CANCELLED"
        self.events.append(f"cancel_invoice {invoice_id}")
        self._commit()
        return dict(inv)

    def simulate_invoice_payment(self, invoice_id):
        inv = self._inv(invoice_id)
        if inv["status"] not in {"SENT", "UNPAID", "PARTIALLY_PAID"}:
            raise PayPalError(f"Invoice {invoice_id} is {inv['status']} and cannot be paid", 422)
        inv["status"] = "PAID"
        inv["due_amount"] = "0.00"
        self.events.append(f"pay_invoice {invoice_id}")
        self._commit()
        return dict(inv)

    # -- Orders
    def create_order(self, items, currency):
        order_id = self._next("ORDER-MOCK-")
        total = sum(Decimal(str(i["unit_amount"])) * int(i["quantity"]) for i in items)
        order = {
            "id": order_id, "status": "CREATED", "approve_url": f"/mock/checkout/{order_id}",
            "amount": money(total, currency)["value"], "currency": currency.upper(),
            "items": [{"name": i["name"], "quantity": int(i["quantity"]),
                       "unit_amount": money(i["unit_amount"], currency)["value"]} for i in items],
            "capture_id": "", "payer_name": "", "payer_email": "",
        }
        self.orders[order_id] = order
        self.events.append(f"create_order {order_id}")
        self._commit()
        return dict(order)

    def _order(self, order_id):
        if order_id not in self.orders:
            raise PayPalError(f"Order {order_id} not found", 404)
        return self.orders[order_id]

    def get_order(self, order_id):
        return dict(self._order(order_id))

    def simulate_buyer_approval(self, order_id):
        order = self._order(order_id)
        if order["status"] != "CREATED":
            raise PayPalError(f"Order {order_id} is {order['status']}", 422)
        order["status"] = "APPROVED"
        order["payer_name"] = "Sandbox Buyer"
        order["payer_email"] = FAKE_BUYER["email_address"]
        self._commit()
        return dict(order)

    def capture_order(self, order_id):
        order = self._order(order_id)
        if order["status"] != "APPROVED":
            raise PayPalError(f"Order {order_id} is {order['status']}; the buyer has not approved it yet", 422)
        order["status"] = "COMPLETED"
        order["capture_id"] = self._next("CAPTURE-MOCK-")
        order["refunded"] = "0.00"
        self.events.append(f"capture_order {order_id}")
        self._commit()
        return dict(order)

    # -- Payments
    def refund_capture(self, capture_id, amount=None, currency="USD", note=""):
        order = next((o for o in self.orders.values() if o["capture_id"] == capture_id), None)
        if not order:
            raise PayPalError(f"Capture {capture_id} not found", 404)
        remaining = Decimal(order["amount"]) - Decimal(order.get("refunded", "0"))
        amt = Decimal(str(amount)) if amount is not None else remaining
        if amt <= 0 or amt > remaining:
            raise PayPalError(f"Refund {amt} exceeds the refundable balance {remaining}", 422)
        order["refunded"] = str(Decimal(order.get("refunded", "0")) + amt)
        refund_id = self._next("REFUND-MOCK-")
        refund = {"id": refund_id, "status": "COMPLETED", "capture_id": capture_id,
                  "amount": money(amt, currency)["value"], "currency": currency.upper()}
        self.refunds[refund_id] = refund
        self.events.append(f"refund {capture_id} {refund['amount']}")
        self._commit()
        return dict(refund)

    # -- Payouts
    def create_payout(self, items, currency, subject="You have a payout"):
        batch_id = self._next("PAYOUT-MOCK-")
        payout = {"batch_id": batch_id, "status": "SUCCESS", "items": items, "currency": currency.upper()}
        self.payouts[batch_id] = payout
        self.events.append(f"payout {batch_id}")
        self._commit()
        return dict(payout)
