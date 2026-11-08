"""Runtime configuration, read from environment variables (and an optional .env file).

Nothing secret is ever hardcoded here. If PayPal credentials are missing, the app runs
against an in-memory PayPal fake. If no LLM key is set, a deterministic intent parser is
used instead of an LLM. MOCK_MODE=1 forces both fakes even when keys are present.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal

try:  # optional dependency; harmless if missing
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover
    pass


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    paypal_client_id: str = ""
    paypal_client_secret: str = ""
    paypal_env: str = "sandbox"
    llm_provider: str = ""  # "openai" | "gemini" | ""
    openai_api_key: str = ""
    gemini_api_key: str = ""
    llm_model: str = ""
    mock_mode: bool = False
    public_base_url: str = "http://localhost:8000"
    default_currency: str = "USD"
    max_amount: Decimal = Decimal("10000")
    brand_name: str = "AgentPay Demo Store"
    db_path: str = ""  # "" = in-memory (tests); load_settings() defaults to data/agentpay.db

    @property
    def paypal_mock(self) -> bool:
        return self.mock_mode or not (self.paypal_client_id and self.paypal_client_secret)

    @property
    def llm_key(self) -> str:
        if self.llm_provider == "openai":
            return self.openai_api_key
        if self.llm_provider == "gemini":
            return self.gemini_api_key
        return ""

    @property
    def llm_enabled(self) -> bool:
        return (not self.mock_mode) and self.llm_provider in {"openai", "gemini"} and bool(self.llm_key)

    @property
    def mode_label(self) -> str:
        return "Mock" if self.paypal_mock else ("Sandbox" if self.paypal_env == "sandbox" else "Live")

    @property
    def llm_label(self) -> str:
        if not self.llm_enabled:
            return "Rule-based parser"
        return f"{ {'openai': 'OpenAI', 'gemini': 'Gemini'}[self.llm_provider] } · {self.llm_model}"

    def describe(self) -> dict:
        return {
            "paypal": "mock (simulated PayPal)" if self.paypal_mock else f"PayPal {self.paypal_env} REST API",
            "paypal_mock": self.paypal_mock,
            "mode": self.mode_label,
            "brain": f"LLM tool-calling ({self.llm_provider})" if self.llm_enabled else "deterministic intent parser",
            "llm_enabled": self.llm_enabled,
            "llm": self.llm_label,
            "llm_model": self.llm_model if self.llm_enabled else "",
            "default_currency": self.default_currency,
            "persistent": bool(self.db_path) and self.db_path != ":memory:",
            "max_amount": str(self.max_amount),
        }


def load_settings() -> Settings:
    provider = os.getenv("LLM_PROVIDER", "").strip().lower()
    default_model = {"openai": "gpt-4o-mini", "gemini": "gemini-3.5-flash-lite"}.get(provider, "")
    return Settings(
        paypal_client_id=os.getenv("PAYPAL_CLIENT_ID", "").strip(),
        paypal_client_secret=os.getenv("PAYPAL_CLIENT_SECRET", "").strip(),
        paypal_env=os.getenv("PAYPAL_ENV", "sandbox").strip().lower() or "sandbox",
        llm_provider=provider,
        openai_api_key=os.getenv("OPENAI_API_KEY", "").strip(),
        gemini_api_key=os.getenv("GEMINI_API_KEY", "").strip(),
        llm_model=os.getenv("LLM_MODEL", "").strip() or default_model,
        mock_mode=_truthy(os.getenv("MOCK_MODE")),
        public_base_url=os.getenv("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/"),
        default_currency=os.getenv("DEFAULT_CURRENCY", "USD").strip().upper() or "USD",
        max_amount=Decimal(os.getenv("MAX_AMOUNT", "10000")),
        brand_name=os.getenv("BRAND_NAME", "AgentPay Demo Store"),
        db_path=os.getenv("DB_PATH", "data/agentpay.db").strip(),
    )
