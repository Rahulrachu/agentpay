"""Tests for v0.2 features: multi-step plans, explanations, audit log, SQLite persistence,
/health, overdue handling, demo seed, and Gemini robustness."""
import json
from datetime import date

import httpx
import pytest
from fastapi.testclient import TestClient

from app.agent import Agent
from app.config import Settings
from app.intent_parser import parse, split_clauses
from app.llm import LLMBrain, LLMEmptyResponse
from app.main import create_app
from app.mock_paypal import MockPayPalClient
from app.store import Store
from app.tools import TOOL_SCHEMAS, ToolCall, Plan

TODAY = date(2026, 11, 8)
WEB = {"x-agentpay-client": "web"}
PLAN_MSG = "Invoice Priya $40 and Arjun $25 for the workshop, remind anyone overdue"


def make(db_path=":memory:", **kw):
    app = create_app(Settings(mock_mode=True, db_path=db_path, **kw))
    app.state.agent.today_fn = lambda: TODAY
    return TestClient(app), app.state.agent


@pytest.fixture
def api():
    return make()


def chat(c, text):
    r = c.post("/api/chat", json={"message": text})
    assert r.status_code == 200
    return r.json()["blocks"]


def first_confirm(blocks):
    return next(b for b in blocks if b["type"] == "confirm")


# ------------------------------------------------------------------ parser: multi-step

def test_parser_splits_multi_part_request():
    plan = parse(PLAN_MSG, today=TODAY)
    assert [c.name for c in plan.tool_calls] == ["create_invoice", "create_invoice", "remind_invoice"]
    assert plan.tool_calls[0].args["recipient_name"] == "Priya" and plan.tool_calls[0].args["amount"] == 40.0
    assert plan.tool_calls[1].args["recipient_name"] == "Arjun" and plan.tool_calls[1].args["amount"] == 25.0
    assert plan.tool_calls[0].args["description"] == plan.tool_calls[1].args["description"] == "the workshop"
    assert plan.tool_calls[2].args == {"invoice_ref": "ALL_OVERDUE"}


def test_parser_multi_invoice_keeps_thousands_separator_and_due_date():
    plan = parse("Invoice Meera ₹1,200 and Kabir ₹800 for the Diwali hamper, due Friday", today=TODAY)
    assert [(c.args["recipient_name"], c.args["amount"], c.args["currency"], c.args["due_date"])
            for c in plan.tool_calls] == [("Meera", 1200.0, "INR", "2026-11-13"), ("Kabir", 800.0, "INR", "2026-11-13")]


def test_parser_does_not_split_single_requests():
    for text in ["Invoice Priya $40 for logo design, due Friday", "Pay $20 to alex@example.com and $10 to sam@example.com",
                 "payment link for 2 T-shirts at $15 and 1 mug at $8", "Remind everyone unpaid"]:
        assert len(split_clauses(text)) == 1, text


def test_parser_then_clause_and_unknown_part_is_reported():
    plan = parse("Make a checkout link for 1 mug at $8 then show unpaid", today=TODAY)
    assert [c.name for c in plan.tool_calls] == ["create_checkout_link", "unpaid_summary"]
    plan = parse("Who hasn't paid?, then list blah", today=TODAY)
    assert plan.tool_calls[0].name == "unpaid_summary"


# ------------------------------------------------------------------ plans + per-step approval

def test_plan_card_with_per_step_approval(api):
    c, agent = api
    c.post("/api/demo/seed")
    blocks = chat(c, PLAN_MSG)
    assert len(blocks) == 1 and blocks[0]["type"] == "plan"
    plan = blocks[0]
    assert [s["status"] for s in plan["steps"]] == ["awaiting", "awaiting", "awaiting"]
    assert all(s["why"] for s in plan["steps"])
    reminder = first_confirm(plan["steps"][2]["blocks"])
    assert {r[0] for r in reminder["details"]} == {"Meera Nair", "Kabir Shah"}  # only overdue, not Priya/Arjun
    # nothing has been sent yet
    assert not any(e.startswith(("send_invoice INV2-MOCK-0006", "remind")) for e in agent.client.events)

    # approve just step 1
    step1 = first_confirm(plan["steps"][0]["blocks"])
    res = c.post("/api/confirm", json={"action_id": step1["action_id"], "approve": True}, headers=WEB).json()
    assert res["plan_id"] == plan["plan_id"] and res["step"] == 1 and res["step_status"] == "done"
    state = c.get(f"/api/plan/{plan['plan_id']}").json()
    assert [s["status"] for s in state["steps"]] == ["done", "awaiting", "awaiting"]

    # approve the rest at once
    res = c.post(f"/api/plan/{plan['plan_id']}/decide", json={"approve": True}, headers=WEB).json()
    assert [r["step"] for r in res["results"]] == [2, 3]
    assert [s["status"] for s in res["plan"]["steps"]] == ["done", "done", "done"]
    assert sum(e.startswith("remind_invoice") for e in agent.client.events) == 2
    statuses = {i["recipient_name"]: i["status"] for i in agent.client.list_invoices()}
    assert statuses["Priya"] == statuses["Arjun"] == "SENT"


def test_plan_cancel_remaining(api):
    c, agent = api
    plan = chat(c, "Invoice Sam $10 and Kim $12 for stickers")[0]
    res = c.post(f"/api/plan/{plan['plan_id']}/decide", json={"approve": False}).json()
    assert [s["status"] for s in res["plan"]["steps"]] == ["declined", "declined"]
    assert {i["status"] for i in agent.client.list_invoices()} == {"DRAFT"}
    again = c.post(f"/api/plan/{plan['plan_id']}/decide", json={"approve": True}).json()
    assert "Nothing in that plan" in again["blocks"][0]["text"]


def test_plan_step_error_does_not_block_other_steps(api):
    c, _ = api
    plan = chat(c, "Invoice Big $50000 for a yacht, then make a checkout link for 1 mug at $8")[0]
    assert [s["status"] for s in plan["steps"]] == ["error", "done"]
    assert "safety cap" in plan["steps"][0]["blocks"][0]["text"]


# ------------------------------------------------------------------ explanations

def test_every_action_explains_why(api):
    c, _ = api
    blocks = chat(c, "Make a checkout link for 2 T-shirts at $15")
    assert "buyer" in blocks[0]["why"]
    blocks = chat(c, "Invoice Priya $40 for logo design")
    assert "draft" in blocks[0]["why"].lower()
    card = first_confirm(blocks)
    assert card["why"] and card["headline"] == {"action": "Send invoice", "amount": "$40.00", "recipient": "Priya"}


def test_llm_reason_is_used_as_explanation(api):
    c, agent = api

    class Brain:
        label = "Fake · test"

        def plan(self, *a, **k):
            return Plan([ToolCall("unpaid_summary", {"reason": "  You want to know   who still owes you. "})])

    agent.brain = Brain()
    blocks = chat(c, "who owes me")
    assert blocks[0]["why"] == "You want to know who still owes you."
    assert c.get("/api/status").json()["last_brain"] == "Fake · test"


def test_every_tool_schema_accepts_reason():
    assert all("reason" in t["parameters"]["properties"] for t in TOOL_SCHEMAS)


# ------------------------------------------------------------------ audit log

def test_audit_log_records_proposal_approval_and_execution(api):
    c, _ = api
    card = first_confirm(chat(c, "Invoice Priya $40 for logo design"))
    c.post("/api/confirm", json={"action_id": card["action_id"], "approve": True}, headers=WEB)
    card2 = first_confirm(chat(c, "Invoice Sam $5 for stickers"))
    c.post("/api/confirm", json={"action_id": card2["action_id"], "approve": False})
    entries = c.get("/api/audit").json()["entries"]
    events = [(e["event"], e["actor"], e["approved_by"]) for e in reversed(entries)]
    assert ("executed", "agent", "auto (safe action, no money moves)") in events  # the draft
    assert ("proposed", "agent", "") in events
    assert ("approved", "seller (web UI)", "seller (web UI)") in events
    sent = next(e for e in entries if e["summary"].startswith("Sent invoice"))
    assert sent["approved_by"] == "seller (web UI)" and sent["action_id"] == card["action_id"] and sent["why"]
    declined = next(e for e in entries if e["event"] == "declined")
    assert declined["actor"] == "seller (API)"
    assert all(e["at"].endswith("Z") and "T" in e["at"] for e in entries)  # ISO 8601 UTC


def test_buyer_capture_is_attributed_to_buyer(api):
    c, _ = api
    link = next(b for b in chat(c, "checkout link for 1 mug at $8") if b["type"] == "link")
    c.post(f"/api/mock/checkout/{link['url'].rsplit('/', 1)[-1]}/pay")
    cap = next(e for e in c.get("/api/audit").json()["entries"] if e["summary"].startswith("Captured"))
    assert cap["approved_by"].startswith("buyer")


# ------------------------------------------------------------------ persistence

def test_state_survives_restart(tmp_path):
    db = str(tmp_path / "agentpay.db")
    c, agent = make(db)
    card = first_confirm(chat(c, "Invoice Priya $40 for logo design, due Friday"))
    link = next(b for b in chat(c, "Make a checkout link for 2 T-shirts at $15") if b["type"] == "link")
    order_id = link["url"].rsplit("/", 1)[-1]
    c.post(f"/api/mock/checkout/{order_id}/pay")
    refund_card = first_confirm(chat(c, "Refund $5 of the last order"))
    agent.store.conn.close()

    c2, agent2 = make(db)  # "restart"
    led = c2.get("/api/ledger").json()
    assert [i["recipient_name"] for i in led["invoices"]] == ["Priya"]
    assert led["orders"][0]["id"] == order_id and led["orders"][0]["status"] == "COMPLETED"
    assert {p["action_id"] for p in led["pending"]} == {card["action_id"], refund_card["action_id"]}
    # pending approvals still work after the restart
    out = c2.post("/api/confirm", json={"action_id": refund_card["action_id"], "approve": True}).json()
    assert out["step_status"] == "done"
    assert agent2.orders[order_id]["refunded"] == "5.00"
    # new mock IDs don't collide with old ones
    new = first_confirm(chat(c2, "Invoice Kim $9 for stickers"))
    assert new["details"][-1][1] not in {i["id"] for i in led["invoices"]}
    # audit history kept
    assert any(e["summary"].startswith("Draft invoice") for e in c2.get("/api/audit").json()["entries"])


def test_store_sync_and_meta_roundtrip():
    s = Store(":memory:")
    s.sync("thing", {"a": {"id": "a", "n": 1}, "b": {"id": "b", "n": 2}})
    s.sync("thing", {"b": {"id": "b", "n": 3}})
    assert s.all("thing") == [{"id": "b", "n": 3}]
    s.set_meta("k", [1, 2])
    assert s.get_meta("k") == [1, 2] and s.get_meta("missing", "d") == "d"
    assert s.ping()


# ------------------------------------------------------------------ ledger, health, status, demo

def test_ledger_flags_overdue_and_totals(api):
    c, _ = api
    c.post("/api/demo/seed")
    led = c.get("/api/ledger").json()
    by = {i["recipient_name"]: i["display_status"] for i in led["invoices"]}
    assert by == {"Meera Nair": "OVERDUE", "Kabir Shah": "OVERDUE", "Ana Lopez": "PAID"}
    assert led["owed"] == {"INR": "1200.00", "USD": "60.00"}
    assert led["counts"]["overdue"] == 2


def test_remind_overdue_when_nobody_is_overdue(api):
    c, _ = api
    blocks = chat(c, "remind anyone overdue")
    assert blocks[0]["type"] == "text" and "Nobody is overdue" in blocks[0]["text"]


def test_health_and_status(api):
    c, _ = api
    h = c.get("/health")
    assert h.status_code == 200 and h.json()["status"] == "ok" and h.json()["mode"] == "Mock"
    s = c.get("/api/status").json()
    assert s["mode"] == "Mock" and s["llm"] == "Rule-based parser"


def test_status_labels_for_sandbox_and_gemini():
    s = Settings(paypal_client_id="id", paypal_client_secret="sec", paypal_env="sandbox",
                 llm_provider="gemini", gemini_api_key="g", llm_model="gemini-2.5-flash")
    d = s.describe()
    assert d["mode"] == "Sandbox" and d["llm"] == "Gemini · gemini-2.5-flash" and d["llm_enabled"]
    assert "g" not in json.dumps({k: v for k, v in d.items() if k != "llm"}).split('"')  # no key in status


def test_demo_seed_only_in_mock_mode():
    app = create_app(Settings(paypal_client_id="id", paypal_client_secret="sec", db_path=":memory:"))
    assert TestClient(app).post("/api/demo/seed").status_code == 404


def test_mock_client_persists_without_agent():
    store = Store(":memory:")
    m = MockPayPalClient(store=store)
    inv = m.create_draft_invoice("A", 5, "USD", "x")
    m2 = MockPayPalClient(store=store)
    assert m2.get_invoice(inv["id"])["status"] == "DRAFT"
    assert m2.create_draft_invoice("B", 6, "USD", "y")["id"] != inv["id"]


# ------------------------------------------------------------------ Gemini robustness

def gemini(handler):
    return LLMBrain("gemini", "g-test", "gemini-2.5-flash", http=httpx.Client(transport=httpx.MockTransport(handler)))


def test_gemini_request_shape_and_multi_call_with_thought_signature():
    seen = {}

    def handler(req):
        seen["url"], seen["body"] = str(req.url), json.loads(req.content)
        return httpx.Response(200, json={"candidates": [{"content": {"role": "model", "parts": [
            {"functionCall": {"name": "create_invoice", "args": {"recipient_name": "Priya", "amount": 40,
                                                                  "description": "workshop", "reason": "Bill Priya."}},
             "thoughtSignature": "abc"},
            {"functionCall": {"name": "remind_invoice", "args": {"invoice_ref": "ALL_OVERDUE"}}}]},
            "finishReason": "STOP"}]})

    brain = gemini(handler)
    plan = brain.plan(PLAN_MSG, history=[{"role": "user", "content": "hi"}, {"role": "assistant", "content": ""}],
                      today=TODAY)
    assert seen["url"].endswith("/v1beta/models/gemini-2.5-flash:generateContent")
    assert [c["role"] for c in seen["body"]["contents"]] == ["user", "user"]  # empty history turn dropped
    assert "reason" in seen["body"]["tools"][0]["functionDeclarations"][0]["parameters"]["properties"]
    assert [c.name for c in plan.tool_calls] == ["create_invoice", "remind_invoice"]
    assert brain.label == "Gemini · gemini-2.5-flash"


@pytest.mark.parametrize("payload", [
    {"candidates": [{"finishReason": "MALFORMED_FUNCTION_CALL"}]},
    {"promptFeedback": {"blockReason": "SAFETY"}},
    {"candidates": [{"content": {"parts": [{"text": "thinking", "thought": True}]}, "finishReason": "STOP"}]},
])
def test_gemini_empty_responses_raise(payload):
    brain = gemini(lambda req: httpx.Response(200, json=payload))
    with pytest.raises(LLMEmptyResponse):
        brain.plan("who hasn't paid?", today=TODAY)


def test_gemini_http_error_falls_back_to_parser_and_is_audited(api):
    c, agent = api
    agent.brain = gemini(lambda req: httpx.Response(429, json={"error": {"status": "RESOURCE_EXHAUSTED"}}))
    blocks = chat(c, PLAN_MSG)
    assert "LLM unavailable: HTTPStatusError" in blocks[0]["text"]
    assert blocks[1]["type"] == "plan" and len(blocks[1]["steps"]) == 3
    assert c.get("/api/status").json()["last_brain"] == "rule-based parser (LLM fallback)"
    assert c.get("/api/audit").json()["entries"][-1]["event"] == "llm_fallback"


def test_gemini_empty_candidate_falls_back(api):
    c, agent = api
    agent.brain = gemini(lambda req: httpx.Response(200, json={"candidates": [{"finishReason": "SAFETY"}]}))
    blocks = chat(c, "Who hasn't paid?")
    assert "LLMEmptyResponse" in blocks[0]["text"] and blocks[1]["type"] == "text"


def test_agent_without_store_argument_still_works():
    agent = Agent(MockPayPalClient(), today_fn=lambda: TODAY)
    out = agent.handle("Invoice Priya $40 for logo design")
    assert first_confirm(out["blocks"])["why"]
