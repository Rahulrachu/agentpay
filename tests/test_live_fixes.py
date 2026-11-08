"""Fixes from live sandbox runs: multi-recipient invoices, reminder wording, friendly PayPal errors."""
from datetime import date

import httpx
import pytest
from fastapi.testclient import TestClient

from app.agent import people_text
from app.config import Settings
from app.intent_parser import parse
from app.main import create_app
from app.paypal_client import PayPalClient, PayPalError

TODAY = date(2026, 11, 8)
LIVE_MSG = "Invoice Meera (meera.demo@example.com) $60 and Kabir (kabir.demo@example.com) $35 for the workshop"


def invoices(text):
    plan = parse(text, TODAY)
    assert all(c.name == "create_invoice" for c in plan.tool_calls)
    return [c.args for c in plan.tool_calls]


# ------------------------------------------------------------------ 1. multi-recipient invoices

def test_live_multi_recipient_message_with_emails():
    a = invoices(LIVE_MSG)
    assert a == [
        {"recipient_name": "Meera", "recipient_email": "meera.demo@example.com", "amount": 60.0,
         "currency": "USD", "description": "the workshop"},
        {"recipient_name": "Kabir", "recipient_email": "kabir.demo@example.com", "amount": 35.0,
         "currency": "USD", "description": "the workshop"},
    ]


def test_multi_recipient_shared_due_date():
    a = invoices(LIVE_MSG + ", due Friday")
    assert [i["due_date"] for i in a] == ["2026-11-13", "2026-11-13"]
    assert [i["recipient_email"] for i in a] == ["meera.demo@example.com", "kabir.demo@example.com"]


def test_multi_recipient_comma_and_ampersand_list_mixed_currency():
    a = invoices("Invoice Priya $40, Arjun $25, and Sam ₹2,500 for the workshop due Nov 20")
    assert [(i["recipient_name"], i["amount"], i["currency"]) for i in a] == [
        ("Priya", 40.0, "USD"), ("Arjun", 25.0, "USD"), ("Sam", 2500.0, "INR")]
    assert {i["description"] for i in a} == {"the workshop"}
    assert {i["due_date"] for i in a} == {"2026-11-20"}
    assert len(invoices("Invoice Priya $40, Arjun $25 & Sam $10 for the workshop")) == 3


def test_multi_recipient_some_with_email_angle_brackets_and_bare():
    a = invoices("Invoice Meera <meera.demo@example.com> $60, Kabir kabir.demo@example.com $35 and Ana $10 for prints")
    assert [(i["recipient_name"], i.get("recipient_email", "")) for i in a] == [
        ("Meera", "meera.demo@example.com"), ("Kabir", "kabir.demo@example.com"), ("Ana", "")]


def test_multi_recipient_own_descriptions():
    a = invoices("Invoice Meera $60 for design and Kabir $35 for the workshop")
    assert [i["description"] for i in a] == ["design", "the workshop"]


def test_multi_recipient_each():
    a = invoices("Invoice Meera (meera.demo@example.com) and Kabir $35 each for the workshop")
    assert [(i["recipient_name"], i["amount"]) for i in a] == [("Meera", 35.0), ("Kabir", 35.0)]
    assert a[0]["recipient_email"] == "meera.demo@example.com" and "recipient_email" not in a[1]


def test_single_invoice_phrasings_unchanged():
    assert invoices("Invoice Arjun (arjun@example.com) ₹2,500 for website fixes, due 2026-11-01") == [
        {"recipient_name": "Arjun", "recipient_email": "arjun@example.com", "amount": 2500.0, "currency": "INR",
         "description": "website fixes", "due_date": "2026-11-01"}]
    assert invoices("Invoice Priya $40 for paint, brushes and canvas")[0]["description"] == "paint, brushes and canvas"
    assert len(invoices("Invoice Priya $40 for shirts and $10 shipping")) == 1


def test_multi_recipient_becomes_plan_with_one_send_card_each():
    app = create_app(Settings(mock_mode=True))
    app.state.agent.today_fn = lambda: TODAY
    c = TestClient(app)
    blocks = c.post("/api/chat", json={"message": LIVE_MSG}).json()["blocks"]
    plan = next(b for b in blocks if b["type"] == "plan")
    assert len(plan["steps"]) == 2 and plan["awaiting"] == 2
    cards = [b for s in plan["steps"] for b in s["blocks"] if b["type"] == "confirm"]
    assert [x["headline"]["recipient"] for x in cards] == ["meera.demo@example.com", "kabir.demo@example.com"]


# ------------------------------------------------------------------ 2. reminder wording

def test_people_text_dedupes_and_counts():
    assert people_text(["Arjun", "Priya", "Priya"]) == "Arjun and Priya (2 invoices)"
    assert people_text(["A", "B", "C"]) == "A, B and C"
    assert people_text(["Priya"]) == "Priya"


def test_remind_all_dedupes_names():
    app = create_app(Settings(mock_mode=True))
    app.state.agent.today_fn = lambda: TODAY
    c = TestClient(app)
    for msg in ["Invoice Arjun $25 for fixes", "Invoice Priya $40 for logo", "Invoice Priya $15 for icons"]:
        card = next(b for b in c.post("/api/chat", json={"message": msg}).json()["blocks"] if b["type"] == "confirm")
        c.post("/api/confirm", json={"action_id": card["action_id"], "approve": True})
    card = next(b for b in c.post("/api/chat", json={"message": "Remind everyone"}).json()["blocks"]
                if b["type"] == "confirm")
    assert card["title"] == "Send a payment reminder to 2 customers (3 invoices)?"
    assert card["headline"]["recipient"] == "Arjun and Priya (2 invoices)"
    done = c.post("/api/confirm", json={"action_id": card["action_id"], "approve": True}).json()["blocks"]
    assert done[0]["text"] == "Reminders sent to Arjun and Priya (2 invoices)."


# ------------------------------------------------------------------ 3. friendly PayPal errors

def _client(status, body):
    def handler(req):
        if req.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3000})
        return httpx.Response(status, json=body)
    return PayPalClient("x", "y", http=httpx.Client(transport=httpx.MockTransport(handler)))


def test_payout_country_restriction_is_friendly():
    c = _client(403, {"name": "AUTHORIZATION_ERROR",
                      "message": "You live in a country that is not allowed to send this payout.",
                      "debug_id": "abc"})
    with pytest.raises(PayPalError) as exc:
        c.create_payout([{"receiver_email": "alex.demo@example.com", "amount": "20"}], "USD")
    assert exc.value.friendly.startswith("Payouts aren't available on this PayPal account")
    assert "US sandbox business account" in str(exc.value)
    assert "PayPal said: You live in a country" in str(exc.value)


def test_inr_foreign_currency_blocked_is_friendly():
    c = _client(422, {"name": "UNPROCESSABLE_ENTITY", "message": "The requested action could not be performed.",
                      "details": [{"issue": "INR_FOREIGN_CURRENCY_BLOCKED",
                                   "description": "Indian merchants cannot accept INR from Indian buyers."}]})
    with pytest.raises(PayPalError) as exc:
        c.send_invoice("INV2-X")
    assert "India-registered merchant can't invoice or charge buyers in India" in str(exc.value)
    assert "Indian merchants cannot accept INR" in str(exc.value)


def test_unknown_errors_keep_original_message():
    c = _client(400, {"name": "INVALID_REQUEST", "message": "Request is not well-formed.",
                      "details": [{"issue": "MISSING_REQUIRED_PARAMETER", "description": "amount is required"}]})
    with pytest.raises(PayPalError) as exc:
        c.list_invoices()
    assert exc.value.friendly == ""
    assert str(exc.value) == "PayPal GET /v2/invoicing/invoices failed: amount is required"


def test_friendly_payout_error_reaches_chat():
    app = create_app(Settings(mock_mode=True))
    agent = app.state.agent

    def refuse(items, currency, **kw):
        raise PayPalError("Payouts aren't available on this PayPal account: ...", 403, friendly="x")
    agent.client.create_payout = refuse
    c = TestClient(app)
    card = next(b for b in c.post("/api/chat", json={"message": "Pay $20 to alex.demo@example.com"}).json()["blocks"]
                if b["type"] == "confirm")
    out = c.post("/api/confirm", json={"action_id": card["action_id"], "approve": True}).json()["blocks"]
    assert out[0]["type"] == "error" and out[0]["text"].startswith("Payouts aren't available")
