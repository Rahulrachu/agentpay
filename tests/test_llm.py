"""LLM brain tests with mocked OpenAI / Gemini HTTP responses (no keys, no network)."""
import json
from datetime import date

import httpx

from app.llm import LLMBrain


def test_openai_tool_calls_parsed():
    seen = {}

    def handler(req):
        seen["body"] = json.loads(req.content)
        seen["auth"] = req.headers["Authorization"]
        return httpx.Response(200, json={"choices": [{"message": {"content": None, "tool_calls": [
            {"type": "function", "function": {"name": "create_invoice", "arguments": json.dumps(
                {"recipient_name": "Priya", "amount": 40, "description": "logo design", "due_date": "2026-11-13"})}},
            {"type": "function", "function": {"name": "not_a_tool", "arguments": "{}"}}]}}]})

    brain = LLMBrain("openai", "sk-test", "gpt-4o-mini", http=httpx.Client(transport=httpx.MockTransport(handler)))
    plan = brain.plan("Invoice Priya $40 for logo design, due Friday", today=date(2026, 11, 8))
    assert [c.name for c in plan.tool_calls] == ["create_invoice"]  # unknown tools are dropped
    assert plan.tool_calls[0].args["due_date"] == "2026-11-13"
    assert seen["auth"] == "Bearer sk-test"
    assert "2026-11-08 (Sunday)" in seen["body"]["messages"][0]["content"]
    assert {t["function"]["name"] for t in seen["body"]["tools"]} >= {"create_invoice", "refund_order"}


def test_gemini_function_call_parsed():
    def handler(req):
        assert req.headers["x-goog-api-key"] == "g-test"
        body = json.loads(req.content)
        assert body["tools"][0]["functionDeclarations"]
        return httpx.Response(200, json={"candidates": [{"content": {"parts": [
            {"functionCall": {"name": "unpaid_summary", "args": {}}}]}}]})

    brain = LLMBrain("gemini", "g-test", "gemini-2.5-flash", http=httpx.Client(transport=httpx.MockTransport(handler)))
    plan = brain.plan("who hasn't paid?")
    assert plan.tool_calls[0].name == "unpaid_summary"


def test_text_only_reply():
    def handler(req):
        return httpx.Response(200, json={"choices": [{"message": {"content": "Which customer?"}}]})

    brain = LLMBrain("openai", "k", "m", http=httpx.Client(transport=httpx.MockTransport(handler)))
    plan = brain.plan("invoice")
    assert plan.tool_calls == [] and plan.reply == "Which customer?"
