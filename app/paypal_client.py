"""Thin PayPal REST client (sandbox by default) covering Invoicing v2, Orders v2,
Payments v2 refunds and Payouts v1.

Every public method returns a small *normalized* dict so the agent and the UI never depend
on PayPal's full response shapes. The raw response is kept under the "raw" key.
The in-memory fake in mock_paypal.py implements the same methods and return shapes.
"""
from __future__ import annotations

import base64
import time
import uuid
from decimal import Decimal
from typing import Any

import httpx

BASE_URLS = {
    "sandbox": "https://api-m.sandbox.paypal.com",
    "live": "https://api-m.paypal.com",
}

UNPAID_INVOICE_STATUSES = {"SENT", "UNPAID", "PARTIALLY_PAID", "PAYMENT_PENDING", "SCHEDULED"}
ZERO_DECIMAL_CURRENCIES = {"JPY", "HUF", "TWD"}


class PayPalError(Exception):
    def __init__(self, message: str, status_code: int | None = None, details: Any = None, friendly: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.details = details
        self.friendly = friendly  # plain-language reason for known account/country restrictions


def money(value: Decimal | str | float, currency: str) -> dict:
    d = Decimal(str(value))
    q = Decimal("1") if currency.upper() in ZERO_DECIMAL_CURRENCIES else Decimal("0.01")
    return {"currency_code": currency.upper(), "value": str(d.quantize(q))}


def split_name(full_name: str) -> dict:
    parts = (full_name or "").strip().split()
    if not parts:
        return {}
    name = {"given_name": parts[0]}
    if len(parts) > 1:
        name["surname"] = " ".join(parts[1:])
    return name


# ---------------------------------------------------------------- friendly errors

# Known account / country restrictions that a seller can't fix by rephrasing. Each entry is
# (matcher on PayPal's issue codes + messages, friendly explanation). The raw PayPal text is kept
# after the explanation so nothing is hidden.
_PAYOUT_COUNTRY = (
    "Payouts aren't available on this PayPal account: PayPal doesn't let business accounts in this "
    "country send payouts (India-registered merchants can't use Payouts). Nothing was sent. To demo "
    "payouts, use API credentials from a US sandbox business account."
)
_INR_BLOCKED = (
    "PayPal won't take this INR payment: an India-registered merchant can't invoice or charge buyers "
    "in India through PayPal (cross-border only). Nothing was sent. Invoice in another currency such as "
    "USD for an international customer, or use a non-India sandbox business account."
)
_CURRENCY_NOT_SUPPORTED = (
    "This PayPal account can't receive that currency. Try the account's own currency (often USD in the "
    "sandbox) or enable the currency in the sandbox business account settings."
)


def friendly_reason(path: str, body: Any, text: str) -> str:
    """Map PayPal error bodies we have seen live to a plain explanation, or '' if unknown."""
    body = body if isinstance(body, dict) else {}
    issues = [str(body.get("name", ""))] + [
        str(d.get("issue", "")) for d in (body.get("details") or []) if isinstance(d, dict)]
    codes = " ".join(issues).upper()
    blob = (text or "").lower()
    if "INR_FOREIGN_CURRENCY_BLOCKED" in codes or "inr_foreign_currency_blocked" in blob:
        return _INR_BLOCKED
    if "/payouts" in path and ("country that is not allowed" in blob or "COUNTRY_NOT_ALLOWED" in codes
                                or "not allowed to send" in blob):
        return _PAYOUT_COUNTRY
    if "CURRENCY_NOT_SUPPORTED" in codes or ("currency code is not valid" in blob and "/invoic" in path):
        return _CURRENCY_NOT_SUPPORTED
    return ""


# ---------------------------------------------------------------- normalizers

def normalize_invoice(raw: dict) -> dict:
    detail = raw.get("detail", {}) or {}
    recipients = raw.get("primary_recipients") or []
    billing = (recipients[0].get("billing_info", {}) if recipients else {}) or {}
    name = billing.get("name", {}) or {}
    full_name = name.get("full_name") or " ".join(
        p for p in [name.get("given_name"), name.get("surname")] if p
    )
    amount = raw.get("amount") or {}
    due = raw.get("due_amount") or {}
    items = raw.get("items") or []
    description = items[0].get("name") if items else (detail.get("note") or "")
    return {
        "id": raw.get("id", ""),
        "number": detail.get("invoice_number", ""),
        "status": raw.get("status", ""),
        "recipient_name": full_name,
        "recipient_email": billing.get("email_address", ""),
        "amount": amount.get("value", ""),
        "currency": amount.get("currency_code") or detail.get("currency_code", ""),
        "due_amount": due.get("value", ""),
        "due_date": (detail.get("payment_term") or {}).get("due_date", ""),
        "description": description,
        "pay_url": (detail.get("metadata") or {}).get("recipient_view_url", ""),
        "raw": raw,
    }


def _link(raw: dict, *rels: str) -> str:
    for link in raw.get("links", []) or []:
        if link.get("rel") in rels:
            return link.get("href", "")
    return ""


def normalize_order(raw: dict) -> dict:
    units = raw.get("purchase_units") or [{}]
    unit = units[0] or {}
    amount = unit.get("amount") or {}
    captures = ((unit.get("payments") or {}).get("captures")) or []
    payer = raw.get("payer") or {}
    payer_name = " ".join(
        p for p in [(payer.get("name") or {}).get("given_name"), (payer.get("name") or {}).get("surname")] if p
    )
    return {
        "id": raw.get("id", ""),
        "status": raw.get("status", ""),
        "approve_url": _link(raw, "payer-action", "approve"),
        "amount": amount.get("value", ""),
        "currency": amount.get("currency_code", ""),
        "items": [
            {
                "name": i.get("name", ""),
                "quantity": int(i.get("quantity", "1")),
                "unit_amount": (i.get("unit_amount") or {}).get("value", ""),
            }
            for i in unit.get("items", []) or []
        ],
        "capture_id": captures[0].get("id", "") if captures else "",
        "payer_name": payer_name,
        "payer_email": payer.get("email_address", ""),
        "raw": raw,
    }


# ---------------------------------------------------------------- client

class PayPalClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        env: str = "sandbox",
        http: httpx.Client | None = None,
        return_url: str = "http://localhost:8000/paypal/return",
        cancel_url: str = "http://localhost:8000/paypal/cancel",
        brand_name: str = "AgentPay",
    ):
        if env not in BASE_URLS:
            raise ValueError(f"PAYPAL_ENV must be one of {sorted(BASE_URLS)}")
        self.client_id = client_id
        self.client_secret = client_secret
        self.base_url = BASE_URLS[env]
        self.http = http or httpx.Client(timeout=30)
        self.return_url = return_url
        self.cancel_url = cancel_url
        self.brand_name = brand_name
        self._token: str | None = None
        self._token_expiry = 0.0

    # -- plumbing
    def _access_token(self) -> str:
        if self._token and time.time() < self._token_expiry - 60:
            return self._token
        basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        resp = self.http.post(
            f"{self.base_url}/v1/oauth2/token",
            data={"grant_type": "client_credentials"},
            headers={"Authorization": f"Basic {basic}", "Accept": "application/json"},
        )
        if resp.status_code != 200:
            raise PayPalError("PayPal authentication failed", resp.status_code, _safe_json(resp))
        body = resp.json()
        self._token = body["access_token"]
        self._token_expiry = time.time() + int(body.get("expires_in", 3000))
        return self._token

    def _request(self, method: str, path: str, json: Any = None, params: dict | None = None,
                 idempotent: bool = False, prefer_representation: bool = False) -> dict:
        headers = {
            "Authorization": f"Bearer {self._access_token()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if idempotent:
            headers["PayPal-Request-Id"] = str(uuid.uuid4())
        if prefer_representation:
            headers["Prefer"] = "return=representation"
        resp = self.http.request(method, f"{self.base_url}{path}", json=json, params=params, headers=headers)
        if resp.status_code >= 400:
            body = _safe_json(resp)
            msg = (body or {}).get("message") or (body or {}).get("error_description") or resp.text[:200]
            details = (body or {}).get("details") or []
            descs = [d.get("description") or d.get("issue") for d in details if isinstance(d, dict)]
            descs = [d for d in descs if d]
            if descs:
                msg = "; ".join(descs)
            friendly = friendly_reason(path, body, f"{msg} {resp.text[:2000]}")
            if friendly:
                raise PayPalError(f"{friendly} (PayPal said: {msg})", resp.status_code, body, friendly=friendly)
            raise PayPalError(f"PayPal {method} {path} failed: {msg}", resp.status_code, body)
        if resp.status_code == 204 or not resp.content:
            return {}
        return resp.json()

    # -- Invoicing v2
    def create_draft_invoice(self, recipient_name: str, amount: Decimal, currency: str, description: str,
                             recipient_email: str = "", due_date: str = "", note: str = "") -> dict:
        billing: dict = {}
        if recipient_name:
            billing["name"] = split_name(recipient_name)
        if recipient_email:
            billing["email_address"] = recipient_email
        detail: dict = {"currency_code": currency.upper()}
        if note:
            detail["note"] = note
        if due_date:
            detail["payment_term"] = {"due_date": due_date}
        body = {
            "detail": detail,
            "primary_recipients": [{"billing_info": billing}] if billing else [],
            "items": [{"name": description[:200] or "Services", "quantity": "1",
                       "unit_amount": money(amount, currency)}],
        }
        raw = self._request("POST", "/v2/invoicing/invoices", json=body, idempotent=True,
                            prefer_representation=True)
        if "id" not in raw:  # minimal response: {"rel":"self","href":".../invoices/INV2-..."}
            href = raw.get("href") or _link(raw, "self")
            invoice_id = href.rstrip("/").split("/")[-1]
            raw = self._request("GET", f"/v2/invoicing/invoices/{invoice_id}")
        return normalize_invoice(raw)

    def get_invoice(self, invoice_id: str) -> dict:
        return normalize_invoice(self._request("GET", f"/v2/invoicing/invoices/{invoice_id}"))

    def send_invoice(self, invoice_id: str, notify_recipient: bool = True) -> dict:
        self._request("POST", f"/v2/invoicing/invoices/{invoice_id}/send",
                      json={"send_to_invoicer": False, "send_to_recipient": notify_recipient},
                      idempotent=True)
        return self.get_invoice(invoice_id)

    def list_invoices(self, page_size: int = 50) -> list[dict]:
        raw = self._request("GET", "/v2/invoicing/invoices",
                            params={"page": 1, "page_size": page_size, "total_required": "true"})
        return [normalize_invoice(i) for i in raw.get("items", [])]

    def remind_invoice(self, invoice_id: str, note: str = "") -> dict:
        body = {"send_to_recipient": True, "subject": "Payment reminder",
                "note": note or "Friendly reminder: this invoice is still open."}
        self._request("POST", f"/v2/invoicing/invoices/{invoice_id}/remind", json=body, idempotent=True)
        return self.get_invoice(invoice_id)

    def cancel_invoice(self, invoice_id: str, note: str = "") -> dict:
        body = {"send_to_recipient": True, "subject": "Invoice cancelled",
                "note": note or "This invoice has been cancelled."}
        self._request("POST", f"/v2/invoicing/invoices/{invoice_id}/cancel", json=body, idempotent=True)
        return self.get_invoice(invoice_id)

    # -- Orders v2
    def create_order(self, items: list[dict], currency: str) -> dict:
        currency = currency.upper()
        total = sum(Decimal(str(i["unit_amount"])) * int(i["quantity"]) for i in items)
        body = {
            "intent": "CAPTURE",
            "purchase_units": [{
                "amount": {**money(total, currency),
                           "breakdown": {"item_total": money(total, currency)}},
                "items": [{"name": i["name"][:127], "quantity": str(int(i["quantity"])),
                           "unit_amount": money(i["unit_amount"], currency)} for i in items],
            }],
            "payment_source": {"paypal": {"experience_context": {
                "brand_name": self.brand_name, "user_action": "PAY_NOW",
                "return_url": self.return_url, "cancel_url": self.cancel_url}}},
        }
        return normalize_order(self._request("POST", "/v2/checkout/orders", json=body, idempotent=True,
                                             prefer_representation=True))

    def get_order(self, order_id: str) -> dict:
        return normalize_order(self._request("GET", f"/v2/checkout/orders/{order_id}"))

    def capture_order(self, order_id: str) -> dict:
        return normalize_order(self._request("POST", f"/v2/checkout/orders/{order_id}/capture",
                                             json={}, idempotent=True))

    # -- Payments v2
    def refund_capture(self, capture_id: str, amount: Decimal | None = None, currency: str = "USD",
                       note: str = "") -> dict:
        body: dict = {}
        if amount is not None:
            body["amount"] = money(amount, currency)
        if note:
            body["note_to_payer"] = note[:255]
        raw = self._request("POST", f"/v2/payments/captures/{capture_id}/refund", json=body, idempotent=True)
        amt = raw.get("amount") or {}
        return {"id": raw.get("id", ""), "status": raw.get("status", ""), "capture_id": capture_id,
                "amount": amt.get("value", str(amount) if amount is not None else ""),
                "currency": amt.get("currency_code", currency), "raw": raw}

    # -- Payouts v1
    def create_payout(self, items: list[dict], currency: str, subject: str = "You have a payout") -> dict:
        body = {
            "sender_batch_header": {"sender_batch_id": f"agentpay-{uuid.uuid4().hex[:12]}",
                                    "email_subject": subject},
            "items": [{"recipient_type": "EMAIL", "receiver": i["receiver_email"],
                       "amount": {"value": money(i["amount"], currency)["value"], "currency": currency.upper()},
                       "note": i.get("note", "Payout via AgentPay")} for i in items],
        }
        raw = self._request("POST", "/v1/payments/payouts", json=body)
        header = raw.get("batch_header", {})
        return {"batch_id": header.get("payout_batch_id", ""), "status": header.get("batch_status", ""),
                "items": items, "currency": currency.upper(), "raw": raw}


def _safe_json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except Exception:
        return None
