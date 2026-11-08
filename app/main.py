"""FastAPI app: JSON API for the chat agent + static single-page UI.

Run:  uvicorn app.main:app --reload
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field

from .agent import Agent, fmt
from .config import Settings, load_settings
from .llm import LLMBrain
from .mock_paypal import MockPayPalClient
from .paypal_client import PayPalClient, PayPalError
from .store import Store

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


def build_agent(settings: Settings) -> Agent:
    store = Store(settings.db_path or ":memory:")
    if settings.paypal_mock:
        client = MockPayPalClient(store=store)
    else:
        client = PayPalClient(
            settings.paypal_client_id, settings.paypal_client_secret, settings.paypal_env,
            return_url=f"{settings.public_base_url}/paypal/return",
            cancel_url=f"{settings.public_base_url}/paypal/cancel",
            brand_name=settings.brand_name,
        )
    brain = None
    if settings.llm_enabled:
        brain = LLMBrain(settings.llm_provider, settings.llm_key, settings.llm_model,
                         default_currency=settings.default_currency)
    return Agent(client, brain, default_currency=settings.default_currency, max_amount=settings.max_amount,
                 store=store)


class ChatIn(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000)


class ConfirmIn(BaseModel):
    action_id: str
    approve: bool


class PlanDecision(BaseModel):
    approve: bool = True


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    agent = build_agent(settings)
    app = FastAPI(title="AgentPay", version="0.2.0")
    app.state.agent = agent
    app.state.settings = settings

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/health")
    def health():
        """Liveness/readiness for Render and Docker. Does not call PayPal or the LLM."""
        try:
            db_ok = agent.store.ping()
        except Exception:
            db_ok = False
        body = {"status": "ok" if db_ok else "degraded", "db": "ok" if db_ok else "error",
                "mode": settings.mode_label, "llm": settings.llm_label, "version": app.version}
        if not db_ok:
            raise HTTPException(503, body)
        return body

    @app.get("/api/llm/models", include_in_schema=False)
    def llm_models():
        """Names of Gemini models this key can use (no secrets), to pick a working LLM_MODEL."""
        if settings.llm_provider != "gemini" or not settings.gemini_api_key:
            return {"models": []}
        import httpx as _httpx
        r = _httpx.get("https://generativelanguage.googleapis.com/v1beta/models",
                       params={"pageSize": 200}, headers={"x-goog-api-key": settings.gemini_api_key}, timeout=20)
        if r.status_code >= 400:
            return {"error": r.status_code, "detail": r.text[:300]}
        return {"models": [m.get("name") for m in r.json().get("models", [])
                           if "generateContent" in (m.get("supportedGenerationMethods") or [])]}

    @app.get("/api/llm/ping", include_in_schema=False)
    def llm_ping(model: str = "", tools: int = 1):
        """Time one tiny tool-calling request to a Gemini model (diagnostics; no secrets returned)."""
        import time as _t
        import httpx as _httpx
        from .tools import TOOL_SCHEMAS
        m = model or settings.llm_model
        body = {"contents": [{"role": "user", "parts": [{"text": "Invoice Priya $40 for logo design"}]}],
                "generationConfig": {"temperature": 0}}
        if tools:
            body["tools"] = [{"functionDeclarations": TOOL_SCHEMAS}]
        t0 = _t.time()
        try:
            r = _httpx.post(f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent",
                            json=body, headers={"x-goog-api-key": settings.gemini_api_key}, timeout=90)
        except Exception as e:
            return {"model": m, "error": type(e).__name__, "seconds": round(_t.time() - t0, 1)}
        out = {"model": m, "status": r.status_code, "seconds": round(_t.time() - t0, 1)}
        try:
            parts = r.json()["candidates"][0]["content"]["parts"]
            out["parts"] = [list(p.keys()) for p in parts]
        except Exception:
            out["body"] = r.text[:300]
        return out

    @app.get("/api/status")
    def status():
        return {**settings.describe(), "last_brain": agent.last_brain}

    @app.post("/api/chat")
    def chat(body: ChatIn):
        return agent.handle(body.message)

    def _approver(request: Request) -> str:
        return "seller (web UI)" if request.headers.get("x-agentpay-client") == "web" else "seller (API)"

    @app.post("/api/confirm")
    def confirm(body: ConfirmIn, request: Request):
        return agent.confirm(body.action_id, body.approve, approver=_approver(request))

    @app.post("/api/plan/{plan_id}/decide")
    def decide_plan(plan_id: str, body: PlanDecision, request: Request):
        return agent.approve_plan(plan_id, body.approve, approver=_approver(request))

    @app.get("/api/plan/{plan_id}")
    def get_plan(plan_id: str):
        plan = agent.get_plan(plan_id)
        if not plan:
            raise HTTPException(404, "No such plan")
        return plan

    @app.get("/api/audit")
    def audit(limit: int = 200):
        """Every proposal, approval, decline, execution and failure, newest first."""
        return {"entries": agent.audit(max(1, min(limit, 1000)))}

    @app.post("/api/demo/seed")
    def demo_seed():
        if not settings.paypal_mock:
            raise HTTPException(404, "Demo data is only available in mock mode")
        return agent.seed_demo()

    @app.get("/api/ledger")
    def ledger():
        try:
            return agent.ledger()
        except PayPalError as e:
            raise HTTPException(502, str(e))

    # ---- real PayPal checkout return (buyer approved) -> auto-capture, back to the app
    @app.get("/paypal/return", include_in_schema=False)
    def paypal_return(token: str):
        try:
            agent.on_buyer_approved(token)
        except PayPalError:
            pass
        return RedirectResponse(f"/?paid={token}")

    @app.get("/paypal/cancel", include_in_schema=False)
    def paypal_cancel(token: str = ""):
        return RedirectResponse(f"/?cancelled={token}")

    # ---- mock-only simulated buyer pages
    def _require_mock():
        if not settings.paypal_mock:
            raise HTTPException(404)
        return agent.client

    @app.get("/mock/checkout/{order_id}", response_class=HTMLResponse, include_in_schema=False)
    def mock_checkout_page(order_id: str):
        client = _require_mock()
        try:
            o = client.get_order(order_id)
        except PayPalError:
            raise HTTPException(404)
        lines = "".join(f"<li>{i['quantity']} × {i['name']} — {fmt(i['unit_amount'], o['currency'])}</li>" for i in o["items"])
        return _buyer_page("Checkout", f"<ul>{lines}</ul><p class=t>Total {fmt(o['amount'], o['currency'])}</p>",
                           o["status"], f"/api/mock/checkout/{order_id}/pay", o["status"] == "CREATED")

    @app.post("/api/mock/checkout/{order_id}/pay")
    def mock_checkout_pay(order_id: str):
        client = _require_mock()
        try:
            client.simulate_buyer_approval(order_id)
            return {"blocks": agent.on_buyer_approved(order_id)}
        except PayPalError as e:
            raise HTTPException(422, str(e))

    @app.get("/mock/invoice/{invoice_id}", response_class=HTMLResponse, include_in_schema=False)
    def mock_invoice_page(invoice_id: str):
        client = _require_mock()
        try:
            i = client.get_invoice(invoice_id)
        except PayPalError:
            raise HTTPException(404)
        body = (f"<p>Invoice {i['id']} from the seller to <b>{i['recipient_name']}</b></p>"
                f"<p>{i['description']}</p><p class=t>{fmt(i['amount'], i['currency'])}</p>"
                f"<p>Due {i['due_date'] or 'on receipt'}</p>")
        return _buyer_page("Invoice", body, i["status"], f"/api/mock/invoice/{invoice_id}/pay", i["status"] == "SENT")

    @app.post("/api/mock/invoice/{invoice_id}/pay")
    def mock_invoice_pay(invoice_id: str):
        client = _require_mock()
        try:
            inv = client.simulate_invoice_payment(invoice_id)
        except PayPalError as e:
            raise HTTPException(422, str(e))
        agent._log(f"{inv['recipient_name']} paid invoice {invoice_id} (simulated)", actor="buyer (simulated)",
                   approved_by="buyer (simulated pay page)")
        return {"status": inv["status"]}

    return app


def _buyer_page(kind: str, body: str, status: str, pay_url: str, can_pay: bool) -> str:
    button = (f"<button onclick=\"fetch('{pay_url}',{{method:'POST'}}).then(r=>r.ok?location.reload():r.json()"
              f".then(j=>alert(j.detail)))\">Pay now (simulated)</button>") if can_pay else ""
    return f"""<!doctype html><html><head><meta charset=utf-8><meta name=viewport content="width=device-width">
<title>Simulated PayPal {kind}</title><style>body{{font-family:system-ui;background:#f5f7fa;display:grid;place-items:center;
min-height:100vh;margin:0}}.c{{background:#fff;border-radius:14px;padding:28px 32px;box-shadow:0 4px 24px #0001;max-width:420px}}
.b{{background:#fff3cd;color:#7a5b00;padding:6px 10px;border-radius:8px;font-size:13px}}.t{{font-size:28px;font-weight:700}}
button{{background:#0070ba;color:#fff;border:0;border-radius:24px;padding:12px 22px;font-size:16px;cursor:pointer;width:100%}}
</style></head><body><div class=c><p class=b>MOCK MODE — simulated PayPal {kind.lower()} page. No real money moves.</p>
{body}<p>Status: <b>{status}</b></p>{button}</div></body></html>"""


app = create_app()
