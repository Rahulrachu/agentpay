"""End-to-end flows through the FastAPI app in MOCK mode (no keys, deterministic parser)."""
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture
def api():
    app = create_app(Settings(mock_mode=True))
    app.state.agent.today_fn = lambda: date(2026, 11, 8)
    return TestClient(app), app.state.agent


def chat(c, text):
    r = c.post("/api/chat", json={"message": text})
    assert r.status_code == 200
    return r.json()["blocks"]


def confirm(c, blocks, approve=True):
    card = next(b for b in blocks if b["type"] == "confirm")
    r = c.post("/api/confirm", json={"action_id": card["action_id"], "approve": approve})
    return r.json()["blocks"]


def test_status_reports_mock(api):
    c, _ = api
    s = c.get("/api/status").json()
    assert s["paypal_mock"] is True and s["llm_enabled"] is False


def test_invoice_draft_then_human_confirmed_send(api):
    c, agent = api
    blocks = chat(c, "Invoice Priya $40 for logo design, due Friday")
    card = next(b for b in blocks if b["type"] == "confirm")
    inv_id = card["details"][-1][1]
    # nothing is sent before the click
    assert agent.client.get_invoice(inv_id)["status"] == "DRAFT"
    assert ["Due", "2026-11-13"] in card["details"]
    out = confirm(c, blocks)
    assert agent.client.get_invoice(inv_id)["status"] == "SENT"
    assert any(b["type"] == "link" and b["url"] == f"/mock/invoice/{inv_id}" for b in out)
    # a confirmation id is single-use
    again = c.post("/api/confirm", json={"action_id": card["action_id"], "approve": True}).json()["blocks"]
    assert again[0]["type"] == "error"


def test_declining_does_nothing(api):
    c, agent = api
    blocks = chat(c, "Invoice Sam $10 for stickers")
    inv_id = next(b for b in blocks if b["type"] == "confirm")["details"][-1][1]
    confirm(c, blocks, approve=False)
    assert agent.client.get_invoice(inv_id)["status"] == "DRAFT"
    assert "send_invoice" not in " ".join(agent.client.events)


def test_checkout_pay_and_refund(api):
    c, agent = api
    blocks = chat(c, "Make a checkout link for 2 T-shirts at $15")
    link = next(b for b in blocks if b["type"] == "link")
    order_id = link["url"].rsplit("/", 1)[-1]
    assert c.get(link["url"]).status_code == 200  # simulated buyer page renders
    # refund before payment is refused
    assert chat(c, f"refund order {order_id}")[0]["type"] == "error"
    # buyer pays on the simulated checkout page -> auto capture
    assert c.post(f"/api/mock/checkout/{order_id}/pay").status_code == 200
    assert agent.orders[order_id]["status"] == "COMPLETED"
    # partial refund
    blocks = chat(c, f"Refund $5 of order {order_id}")
    assert next(b for b in blocks if b["type"] == "confirm")["title"].startswith("Partial refund of $5.00")
    confirm(c, blocks)
    # refund the rest of the last order
    blocks = chat(c, "refund the last order")
    assert "$25.00" in next(b for b in blocks if b["type"] == "confirm")["title"]
    confirm(c, blocks)
    led = c.get("/api/ledger").json()
    assert led["orders"][0]["display_status"] == "REFUNDED"
    # over-refund is refused
    assert chat(c, f"refund order {order_id}")[0]["type"] == "error"


def test_unpaid_summary_remind_cancel(api):
    c, agent = api
    confirm(c, chat(c, "Invoice Priya $40 for logo design, due Friday"))
    confirm(c, chat(c, "Invoice Arjun ₹2,500 for website fixes, due 2026-11-01"))
    blocks = chat(c, "Invoice Paid Person $5 for test")
    confirm(c, blocks)
    paid_id = [i for i in agent.client.list_invoices() if i["recipient_name"] == "Paid Person"][0]["id"]
    c.post(f"/api/mock/invoice/{paid_id}/pay")
    chat(c, "Invoice Draft Only $9 for later")  # left as draft
    chat(c, "checkout link for 1 mug at $8")  # unpaid link

    summary = chat(c, "Who hasn't paid?")
    text = summary[0]["text"]
    assert "$40.00" in text and "₹2,500.00" in text
    assert "1 checkout link" in text and "1 draft" in text
    rows = next(b for b in summary if b["type"] == "table")["rows"]
    who = [r[0] for r in rows]
    assert "Priya" in who and "Arjun" in who and "Paid Person" not in who
    assert next(r for r in rows if r[0] == "Arjun")[3] == "OVERDUE 7d"
    assert next(r for r in rows if r[0] == "Priya")[3] == "due in 5d"

    reminder = chat(c, "Remind everyone")
    assert "2 customers" in reminder[0]["title"]
    confirm(c, reminder)
    assert sum(e.startswith("remind_invoice") for e in agent.client.events) == 2

    confirm(c, chat(c, "cancel Arjun's invoice"))
    statuses = {i["recipient_name"]: i["status"] for i in agent.client.list_invoices()}
    assert statuses["Arjun"] == "CANCELLED"


def test_payout_requires_confirmation(api):
    c, agent = api
    blocks = chat(c, "Pay $20 to alex@example.com and $10 to sam@example.com")
    assert not agent.client.payouts
    assert "$30.00" in blocks[0]["title"]
    confirm(c, blocks)
    assert len(agent.client.payouts) == 1


def test_safety_cap_and_validation(api):
    c, _ = api
    assert chat(c, "Invoice Big Spender $50000 for a yacht")[0]["type"] == "error"
    assert chat(c, "refund order NOPE123")[0]["type"] == "error"


def test_llm_failure_falls_back_to_parser(api):
    c, agent = api

    class Broken:
        def plan(self, *a, **k):
            raise ConnectionError("no network")

    agent.brain = Broken()
    blocks = chat(c, "Who hasn't paid?")
    assert "LLM unavailable" in blocks[0]["text"]
    assert blocks[1]["type"] == "text"
