"""LLM "brain": turns a seller's message into ToolCalls via native function calling.

Supports OpenAI (Chat Completions tools) and Google Gemini (generateContent
functionDeclarations) over plain HTTPS, so no vendor SDK is required. The LLM only
*proposes* actions; agent.py validates arguments and gates money-moving tools behind a
human confirmation click, so a model mistake cannot move money on its own.
"""
from __future__ import annotations

import json
from datetime import date

import httpx

from .tools import TOOL_NAMES, TOOL_SCHEMAS, Plan, ToolCall


def system_prompt(today: date, default_currency: str) -> str:
    return (
        "You are AgentPay, a payments assistant for small sellers and freelancers. "
        "Map the seller's request to one or more of the provided PayPal tools. "
        f"Today is {today.isoformat()} ({today.strftime('%A')}). Resolve relative due dates to YYYY-MM-DD. "
        f"Default currency is {default_currency} unless the seller states another (₹ or Rs means INR). "
        "Never invent customer emails, amounts or IDs; leave optional fields out if not given. "
        "A request with several parts (e.g. invoice two people, then remind anyone overdue) needs one tool call "
        "per part, in the order the seller gave them. Fill each call's 'reason' with one short sentence the "
        "seller will read, saying why you chose that action. "
        "If the request is unclear or not about payments, reply briefly in text without calling a tool."
    )


class LLMHTTPError(RuntimeError):
    pass


class LLMEmptyResponse(ValueError):
    """The model returned neither a tool call nor text (blocked, malformed call, empty candidate).
    The agent treats this like any other LLM failure and falls back to the rule-based parser."""


class LLMBrain:
    def __init__(self, provider: str, api_key: str, model: str, http: httpx.Client | None = None,
                 default_currency: str = "USD"):
        self.provider = provider
        self.api_key = api_key
        self.model = model
        self.http = http or httpx.Client(timeout=45)
        self.default_currency = default_currency
        self.label = f"{ {'openai': 'OpenAI', 'gemini': 'Gemini'}.get(provider, provider) } · {model}"

    def plan(self, message: str, history: list[dict] | None = None, today: date | None = None) -> Plan:
        today = today or date.today()
        if self.provider == "openai":
            return self._openai(message, history or [], today)
        if self.provider == "gemini":
            return self._gemini(message, history or [], today)
        raise ValueError(f"Unknown LLM provider {self.provider}")

    # -- OpenAI
    def _openai(self, message: str, history: list[dict], today: date) -> Plan:
        messages = [{"role": "system", "content": system_prompt(today, self.default_currency)}]
        messages += [{"role": h["role"], "content": h["content"]} for h in history[-6:]]
        messages.append({"role": "user", "content": message})
        body = {
            "model": self.model,
            "messages": messages,
            "tools": [{"type": "function", "function": t} for t in TOOL_SCHEMAS],
            "tool_choice": "auto",
            "temperature": 0,
        }
        resp = self.http.post("https://api.openai.com/v1/chat/completions", json=body,
                              headers={"Authorization": f"Bearer {self.api_key}"})
        resp.raise_for_status()
        msg = resp.json()["choices"][0]["message"]
        calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            if fn.get("name") in TOOL_NAMES:
                calls.append(ToolCall(fn["name"], json.loads(fn.get("arguments") or "{}")))
        if not calls and not (msg.get("content") or "").strip():
            raise LLMEmptyResponse(f"OpenAI returned no tool call or text "
                                   f"(finish_reason={resp.json()['choices'][0].get('finish_reason')})")
        return Plan(calls, msg.get("content") or "")

    # -- Gemini
    def _gemini(self, message: str, history: list[dict], today: date) -> Plan:
        contents = [{"role": "model" if h["role"] == "assistant" else "user", "parts": [{"text": h["content"]}]}
                    for h in history[-6:] if (h.get("content") or "").strip()]
        contents.append({"role": "user", "parts": [{"text": message}]})
        body = {
            "systemInstruction": {"parts": [{"text": system_prompt(today, self.default_currency)}]},
            "contents": contents,
            "tools": [{"functionDeclarations": TOOL_SCHEMAS}],
            "generationConfig": {"temperature": 0},
        }
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        resp = self.http.post(url, json=body, headers={"x-goog-api-key": self.api_key})
        if resp.status_code >= 400:
            try:
                err = (resp.json().get("error") or {})
                detail = f"{err.get('status', '')}: {str(err.get('message', ''))[:200]}"
            except Exception:
                detail = resp.text[:200]
            raise LLMHTTPError(f"Gemini HTTP {resp.status_code} {detail}")
        data = resp.json()
        candidate = (data.get("candidates") or [{}])[0] or {}
        parts = (candidate.get("content") or {}).get("parts") or []
        calls, text = [], []
        for p in parts:
            if p.get("thought"):  # thinking summaries, if ever returned, are not shown to the seller
                continue
            if "functionCall" in p and p["functionCall"].get("name") in TOOL_NAMES:
                calls.append(ToolCall(p["functionCall"]["name"], dict(p["functionCall"].get("args") or {})))
            elif p.get("text"):
                text.append(p["text"])
        if not calls and not text:
            reason = candidate.get("finishReason") or (data.get("promptFeedback") or {}).get("blockReason") or "empty"
            raise LLMEmptyResponse(f"Gemini returned no tool call or text (finishReason={reason})")
        return Plan(calls, " ".join(text).strip())
