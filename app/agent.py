"""The AgentPay agent: plan -> validate -> (confirm) -> execute -> respond.

* Requests with several parts become a *plan card*: one step per action, each with a one-line
  explanation of why the agent chose it, approved step by step (or all at once).
* Every proposal, approval, decline, execution and failure is written to an audit log
  (SQLite, see store.py) with a timestamp and who approved it.

* A "brain" (LLM tool-calling, or the deterministic parser) proposes ToolCalls.
* Every argument is validated server-side (amount caps, emails, quantities).
* Safe actions (draft invoice, checkout link, summaries) run immediately.
* Money-moving / customer-messaging actions (send, remind, cancel, refund, payout) are
  resolved to concrete PayPal objects and parked as a PendingAction. They only execute when
  the seller clicks Approve on the confirmation card (POST /api/confirm with its one-time id).
"""
from __future__ import annotations

import re
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Callable

from . import intent_parser
from .explain import explain
from .paypal_client import UNPAID_INVOICE_STATUSES, PayPalError
from .store import Store, utcnow
from .tools import CONFIRM_TOOLS, TOOL_NAMES, Plan, ToolCall

EMAIL_RE = re.compile(r"^[\w.+-]+@[\w-]+(\.[\w-]+)+$")
SYMBOL = {"USD": "$", "INR": "₹", "EUR": "€", "GBP": "£"}


def fmt(value, currency: str) -> str:
    try:
        d = Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return f"{currency} {value}"
    sym = SYMBOL.get((currency or "").upper())
    return f"{sym}{d:,}" if sym else f"{currency} {d:,}"


def human_list(items: list[str]) -> str:
    """['A'] -> 'A'; ['A', 'B'] -> 'A and B'; ['A', 'B', 'C'] -> 'A, B and C'."""
    items = [i for i in items if i]
    if len(items) <= 1:
        return "".join(items)
    return f"{', '.join(items[:-1])} and {items[-1]}"


def people_text(names: list[str]) -> str:
    """Deduplicate recipient names, keep first-seen order, count repeats: 'Arjun and Priya (2 invoices)'."""
    counts: dict[str, int] = {}
    for n in names:
        counts[n] = counts.get(n, 0) + 1
    return human_list([f"{n} ({c} invoices)" if c > 1 else n for n, c in counts.items()])


def _public(d: dict) -> dict:
    return {k: v for k, v in d.items() if k != "raw"}


DANGER_TOOLS = {"refund_order", "create_payout", "cancel_invoice"}
STEP_LABELS = {
    "create_invoice": "Draft invoice", "send_invoice": "Send invoice", "list_invoices": "List invoices",
    "unpaid_summary": "Check who hasn't paid", "remind_invoice": "Send reminders", "cancel_invoice": "Cancel invoice",
    "create_checkout_link": "Create checkout link", "capture_order": "Capture payment", "refund_order": "Refund",
    "create_payout": "Payout",
}


@dataclass
class PendingAction:
    id: str
    tool: str
    args: dict
    title: str
    details: list
    why: str = ""
    plan_id: str = ""
    step: int = 0
    headline: dict = field(default_factory=dict)  # {"action", "amount", "recipient"} for the approval card
    created_at: str = field(default_factory=utcnow)

    def card(self) -> dict:
        return {"type": "confirm", "action_id": self.id, "tool": self.tool, "title": self.title,
                "details": self.details, "danger": self.tool in DANGER_TOOLS, "why": self.why,
                "headline": self.headline,
                "plan_id": self.plan_id, "step": self.step}


class AgentError(Exception):
    pass


class Agent:
    def __init__(self, client, brain=None, default_currency: str = "USD", max_amount: Decimal = Decimal("10000"),
                 today_fn: Callable[[], date] = date.today, store: Store | None = None):
        self.client = client
        self.brain = brain
        self.default_currency = default_currency
        self.max_amount = max_amount
        self.today_fn = today_fn
        self.store = store or Store(":memory:")
        self.lock = threading.RLock()
        # state survives restarts: everything below is reloaded from SQLite
        self.orders: dict[str, dict] = {o["id"]: o for o in self.store.all("order")}
        self.pending: dict[str, PendingAction] = {p["id"]: PendingAction(**p) for p in self.store.all("pending")}
        self.plans: dict[str, dict] = {p["id"]: p for p in self.store.all("plan")}
        self.history: list[dict] = self.store.get_meta("history", [])
        self.last_brain = ""  # which brain planned the most recent message
        self._ctx: dict = {}  # who/why for the action currently executing (read by _log)

    def _persist(self) -> None:
        self.store.sync("order", self.orders)
        self.store.sync("pending", {k: asdict(v) for k, v in self.pending.items()})
        self.store.sync("plan", self.plans)
        self.store.set_meta("history", self.history[-20:])

    # ------------------------------------------------------------- entry points
    def handle(self, message: str) -> dict:
        with self.lock:
            blocks: list[dict] = []
            plan = self._plan(message, blocks)
            calls = [c for c in plan.tool_calls]
            if not calls:
                blocks.append({"type": "text", "text": plan.reply or intent_parser.HELP})
            elif len(calls) == 1:
                if plan.reply:
                    blocks.append({"type": "text", "text": plan.reply})
                blocks += self._run_step(calls[0])
            else:
                blocks.append(self._run_plan(message, calls, plan.reply))
            self.history.append({"role": "user", "content": message})
            summary = " ".join(b.get("text", b.get("title", "")) for b in blocks)[:500]
            self.history.append({"role": "assistant", "content": summary or "(done)"})
            self._persist()
            return {"blocks": blocks}

    def _run_step(self, call: ToolCall, plan_id: str = "", step: int = 0) -> list[dict]:
        """Run one tool call: safe ones execute now, gated ones become a confirmation card."""
        args = dict(call.args or {})
        why = explain(call.name, args, reason=args.pop("reason", ""))
        call = ToolCall(call.name, args)
        self._ctx = {"why": why, "plan_id": plan_id, "step": step, "tool": call.name,
                     "approved_by": "auto (safe action, no money moves)"}
        try:
            out = self.dispatch(call)
        except (PayPalError, AgentError, ValueError) as e:
            self.store.add_audit(actor="agent", event="failed", tool=call.name, summary=str(e), why=why,
                                 plan_id=plan_id)
            return [{"type": "error", "text": str(e), "why": why}]
        finally:
            self._ctx = {}
        for b in out:
            if b["type"] == "confirm" and not b.get("why"):
                b["why"] = why
                self.pending[b["action_id"]].why = why
        if out and not any(b.get("why") for b in out):
            out[0]["why"] = why
        return out

    def _run_plan(self, message: str, calls: list[ToolCall], note: str = "") -> dict:
        plan_id = "plan-" + uuid.uuid4().hex[:8]
        steps = []
        for n, call in enumerate(calls, 1):
            blocks = self._run_step(call, plan_id, n)
            confirms = [b for b in blocks if b["type"] == "confirm"]
            status = "error" if any(b["type"] == "error" for b in blocks) else ("awaiting" if confirms else "done")
            why = next((b.get("why") for b in blocks if b.get("why")), "")
            steps.append({"n": n, "tool": call.name, "label": STEP_LABELS.get(call.name, call.name),
                          "summary": self._step_summary(call), "why": why, "status": status,
                          "action_ids": [b["action_id"] for b in confirms], "blocks": blocks})
        plan = {"id": plan_id, "request": message, "created_at": utcnow(),
                "note": note, "steps": steps}
        self.plans[plan_id] = plan
        self.store.add_audit(actor="agent", event="planned", summary=f"{len(steps)}-step plan for: {message}",
                             plan_id=plan_id, detail=[s["summary"] for s in steps])
        return self.plan_card(plan)

    def plan_card(self, plan: dict) -> dict:
        awaiting = sum(1 for s in plan["steps"] if s["status"] == "awaiting")
        return {"type": "plan", "plan_id": plan["id"], "title": f"Plan · {len(plan['steps'])} steps",
                "request": plan["request"], "note": plan.get("note", ""), "awaiting": awaiting,
                "steps": [{k: v for k, v in s.items()} for s in plan["steps"]]}

    @staticmethod
    def _step_summary(call: ToolCall) -> str:
        a = call.args or {}
        if call.name == "create_invoice":
            return f"Invoice {a.get('recipient_name', '?')} {fmt(a.get('amount', 0), (a.get('currency') or 'USD'))}" + \
                   (f" for {a['description']}" if a.get("description") else "")
        if call.name == "remind_invoice":
            ref = (a.get("invoice_ref") or "").upper()
            return {"ALL_OVERDUE": "Remind everyone overdue", "ALL_UNPAID": "Remind everyone unpaid"}.get(
                ref, f"Remind {a.get('invoice_ref')}")
        if call.name in {"refund_order", "capture_order"}:
            return f"{STEP_LABELS[call.name]} {a.get('order_ref', 'last order')}"
        if call.name in {"send_invoice", "cancel_invoice"}:
            return f"{STEP_LABELS[call.name]}: {a.get('invoice_ref')}"
        if call.name == "create_payout":
            return f"Payout to {len(a.get('items') or [])} recipient(s)"
        return STEP_LABELS.get(call.name, call.name)

    def confirm(self, action_id: str, approve: bool, approver: str = "seller (web UI)") -> dict:
        with self.lock:
            out = self._confirm(action_id, approve, approver)
            self._persist()
            return out

    def _confirm(self, action_id: str, approve: bool, approver: str) -> dict:
        action = self.pending.pop(action_id, None)
        if not action:
            return {"blocks": [{"type": "error", "text": "That confirmation has expired or was already used."}]}
        common = dict(tool=action.tool, action_id=action.id, plan_id=action.plan_id, why=action.why)
        if not approve:
            self.store.add_audit(actor=approver, event="declined", summary=f"Declined: {action.title}",
                                 approved_by="", **common)
            self._update_plan_step(action, "declined")
            return {"blocks": [{"type": "text", "text": f"Okay, cancelled. Nothing was sent. ({action.title})"}],
                    "plan_id": action.plan_id, "step": action.step, "step_status": "declined"}
        self.store.add_audit(actor=approver, event="approved", summary=f"Approved: {action.title}",
                             approved_by=approver, **common)
        self._ctx = {"why": action.why, "plan_id": action.plan_id, "tool": action.tool, "approved_by": approver,
                     "action_id": action.id}
        try:
            blocks = getattr(self, f"_exec_{action.tool}")(action.args)
            status = "done"
        except (PayPalError, AgentError, ValueError) as e:
            self.store.add_audit(actor="agent", event="failed", summary=f"{action.title} failed: {e}",
                                 approved_by=approver, **common)
            blocks, status = [{"type": "error", "text": str(e)}], "error"
        finally:
            self._ctx = {}
        self._update_plan_step(action, status)
        return {"blocks": blocks, "plan_id": action.plan_id, "step": action.step, "step_status": status}

    def _update_plan_step(self, action: PendingAction, outcome: str) -> None:
        plan = self.plans.get(action.plan_id)
        if not plan:
            return
        for step in plan["steps"]:
            if action.id in step.get("action_ids", []):
                remaining = [a for a in step["action_ids"] if a in self.pending]
                step["status"] = outcome if outcome != "done" or not remaining else "awaiting"

    def approve_plan(self, plan_id: str, approve: bool = True, approver: str = "seller (web UI)") -> dict:
        """Approve (or cancel) every step of a plan that is still waiting, in step order."""
        with self.lock:
            plan = self.plans.get(plan_id)
            if not plan:
                return {"blocks": [{"type": "error", "text": "That plan no longer exists."}]}
            blocks, results = [], []
            for step in plan["steps"]:
                for action_id in list(step.get("action_ids", [])):
                    if action_id in self.pending:
                        res = self._confirm(action_id, approve, approver)
                        blocks += res["blocks"]
                        results.append({"step": step["n"], "action_id": action_id,
                                        "status": res.get("step_status", "error"), "blocks": res["blocks"]})
            if not blocks:
                blocks = [{"type": "text", "text": "Nothing in that plan is waiting for approval."}]
            self._persist()
            return {"blocks": blocks, "results": results, "plan": self.plan_card(plan)}

    def get_plan(self, plan_id: str) -> dict | None:
        with self.lock:
            plan = self.plans.get(plan_id)
            return self.plan_card(plan) if plan else None

    def _plan(self, message: str, blocks: list) -> Plan:
        today = self.today_fn()
        if self.brain is not None:
            try:
                plan = self.brain.plan(message, self.history, today=today)
                self.last_brain = getattr(self.brain, "label", "LLM")
                return plan
            except Exception as e:  # network / quota / parse problems: degrade gracefully
                blocks.append({"type": "text",
                               "text": f"(LLM unavailable: {type(e).__name__}; using the built-in parser instead.)"})
                self.store.add_audit(actor="system", event="llm_fallback",
                                     summary=f"LLM call failed ({type(e).__name__}); used the rule-based parser")
                self.last_brain = "rule-based parser (LLM fallback)"
        else:
            self.last_brain = "rule-based parser"
        return intent_parser.parse(message, today=today, default_currency=self.default_currency)

    def dispatch(self, call: ToolCall) -> list[dict]:
        if call.name not in TOOL_NAMES:
            raise AgentError(f"Unknown action {call.name}")
        args = dict(call.args or {})
        args.pop("reason", None)
        if call.name in CONFIRM_TOOLS:
            return getattr(self, f"_propose_{call.name}")(args)
        return getattr(self, f"_do_{call.name}")(args)

    # ------------------------------------------------------------- validation
    def _amount(self, value, field_name="amount") -> Decimal:
        try:
            d = Decimal(str(value))
        except (InvalidOperation, ValueError):
            raise AgentError(f"'{value}' is not a valid {field_name}.")
        if d <= 0:
            raise AgentError(f"The {field_name} must be greater than zero.")
        if d > self.max_amount:
            raise AgentError(f"{d} is above this app's safety cap of {self.max_amount}. Change MAX_AMOUNT to allow it.")
        return d.quantize(Decimal("0.01"))

    def _currency(self, value) -> str:
        cur = (value or self.default_currency).strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", cur):
            raise AgentError(f"'{value}' is not a valid currency code.")
        return cur

    @staticmethod
    def _email(value) -> str:
        value = (value or "").strip()
        if value and not EMAIL_RE.match(value):
            raise AgentError(f"'{value}' doesn't look like an email address.")
        return value

    def _new_pending(self, tool, args, title, details, why: str = "", headline: dict | None = None) -> list[dict]:
        ctx = self._ctx
        if not why:
            why = explain(tool, args) if ctx.get("tool") != tool else ctx.get("why", "")
        action = PendingAction(uuid.uuid4().hex[:12], tool, args, title, details, why=why,
                               plan_id=ctx.get("plan_id", ""), step=ctx.get("step", 0), headline=headline or {})
        self.pending[action.id] = action
        self.store.add_audit(actor="agent", event="proposed", tool=tool, summary=f"Waiting for approval: {title}",
                             action_id=action.id, plan_id=action.plan_id, why=why, detail=details)
        return [action.card()]

    def _log(self, text: str, actor: str = "agent", approved_by: str | None = None):
        """Record something that actually happened (an executed PayPal call or a buyer event)."""
        ctx = self._ctx
        self.store.add_audit(actor=actor, event="executed", tool=ctx.get("tool", ""), summary=text,
                             approved_by=ctx.get("approved_by", "") if approved_by is None else approved_by,
                             action_id=ctx.get("action_id", ""), plan_id=ctx.get("plan_id", ""),
                             why=ctx.get("why", ""))

    @property
    def activity(self) -> list[dict]:
        return [{"at": r["at"], "text": r["summary"], "event": r["event"]} for r in self.store.audit(50)][::-1]

    # ------------------------------------------------------------- resolution
    def _resolve_invoice(self, ref: str, statuses: set | None = None) -> dict:
        ref = (ref or "").strip()
        if not ref:
            raise AgentError("Which invoice? Give a customer name or invoice ID.")
        invoices = self.client.list_invoices()
        for inv in invoices:
            if ref.upper() in {inv["id"].upper(), (inv.get("number") or "").upper()}:
                return inv
        matches = [i for i in invoices if ref.lower() in (i.get("recipient_name") or "").lower()
                   or ref.lower() == (i.get("recipient_email") or "").lower()]
        if statuses:
            preferred = [i for i in matches if i["status"] in statuses]
            matches = preferred or matches
        if not matches:
            raise AgentError(f"I couldn't find an invoice for '{ref}'.")
        return matches[-1]

    def _resolve_order(self, ref: str, statuses: set | None = None) -> dict:
        ref = (ref or "").strip()
        orders = list(self.orders.values())
        if ref.upper() in {"LAST", "LATEST", ""}:
            cands = [o for o in orders if not statuses or o["status"] in statuses]
            if not cands:
                raise AgentError("There's no matching recent order yet.")
            return cands[-1]
        for o in orders:
            if o["id"].upper() == ref.upper() or (o.get("capture_id") or "").upper() == ref.upper():
                return o
        cands = [o for o in orders if ref.lower() in (o.get("payer_name") or "").lower()
                 or any(ref.lower() in i["name"].lower() for i in o.get("items", []))]
        if statuses:
            cands = [o for o in cands if o["status"] in statuses] or cands
        if cands:
            return cands[-1]
        if re.fullmatch(r"[A-Z0-9-]{8,}", ref.upper()):  # an order created outside this session
            try:
                order = _public(self.client.get_order(ref.upper()))
                order.setdefault("refunded", "0.00")
                self.orders[order["id"]] = order
                return order
            except PayPalError:
                pass
        raise AgentError(f"I couldn't find an order matching '{ref}'.")

    # ------------------------------------------------------------- safe actions
    def _do_create_invoice(self, a: dict) -> list[dict]:
        name = (a.get("recipient_name") or "").strip()
        if not name:
            raise AgentError("Who is the invoice for?")
        amount = self._amount(a.get("amount"))
        currency = self._currency(a.get("currency"))
        email = self._email(a.get("recipient_email"))
        desc = (a.get("description") or "Services").strip()
        due = (a.get("due_date") or "").strip()
        if due and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", due):
            raise AgentError(f"Due date '{due}' should be YYYY-MM-DD.")
        inv = self.client.create_draft_invoice(recipient_name=name, amount=amount, currency=currency,
                                               description=desc, recipient_email=email, due_date=due)
        self._log(f"Draft invoice {inv['id']} for {name} ({fmt(amount, currency)})")
        due_txt = f", due {datetime.fromisoformat(due).strftime('%a %d %b %Y')}" if due else ""
        blocks = [{"type": "text",
                   "text": f"Drafted invoice {inv['id']} for {name}: {fmt(amount, currency)} for {desc}{due_txt}."}]
        who = inv.get("recipient_email") or name
        send_why = (f"Sending emails {who} on PayPal's behalf, so it waits for your click."
                    if inv.get("recipient_email") else
                    f"Sending makes the invoice payable and gives you a pay link for {name}, so it waits for your click.")
        blocks[0]["why"] = self._ctx.get("why", "")
        blocks += self._propose_send_invoice({"invoice_ref": inv["id"]}, invoice=inv, why=send_why)
        return blocks

    def _do_list_invoices(self, a: dict) -> list[dict]:
        status = (a.get("status") or "").upper()
        invoices = [i for i in self.client.list_invoices() if not status or i["status"] == status]
        if not invoices:
            return [{"type": "text", "text": "No invoices yet." if not status else f"No {status.lower()} invoices."}]
        rows = [[i["recipient_name"] or "—", fmt(i["amount"], i["currency"]), i["status"], i.get("due_date") or "—",
                 i["id"]] for i in invoices]
        return [{"type": "table", "title": f"{len(rows)} invoice(s)",
                 "columns": ["Customer", "Amount", "Status", "Due", "ID"], "rows": rows}]

    def _do_unpaid_summary(self, a: dict) -> list[dict]:
        today = self.today_fn()
        invoices = [i for i in self.client.list_invoices() if i["status"] in UNPAID_INVOICE_STATUSES]
        drafts = [i for i in self.client.list_invoices() if i["status"] == "DRAFT"]
        open_links = [o for o in self.orders.values() if o["status"] in {"CREATED", "APPROVED", "PAYER_ACTION_REQUIRED"}]
        if not invoices and not open_links:
            text = "Everyone has paid. No open invoices or unpaid checkout links."
            if drafts:
                text += f" ({len(drafts)} draft invoice(s) not sent yet.)"
            return [{"type": "text", "text": text}]
        totals: dict[str, Decimal] = {}
        rows = []
        for i in invoices:
            owed = Decimal(i.get("due_amount") or i["amount"] or "0")
            totals[i["currency"]] = totals.get(i["currency"], Decimal("0")) + owed
            state = i["status"]
            if i.get("due_date"):
                days = (today - date.fromisoformat(i["due_date"])).days
                state = f"OVERDUE {days}d" if days > 0 else ("due today" if days == 0 else f"due in {-days}d")
            rows.append([i["recipient_name"] or i.get("recipient_email") or "—", fmt(owed, i["currency"]),
                         "Invoice", state, i["id"]])
        for o in open_links:
            rows.append(["Checkout link", fmt(o["amount"], o["currency"]), "Order",
                         "buyer hasn't paid" if o["status"] != "APPROVED" else "approved, not captured", o["id"]])
        owed_txt = " + ".join(fmt(v, k) for k, v in totals.items()) or "nothing on invoices"
        n = len({r[0] for r in rows if r[2] == "Invoice"})
        text = f"You're owed {owed_txt} across {len(invoices)} open invoice(s) from {n} customer(s)."
        if open_links:
            text += f" {len(open_links)} checkout link(s) are still unpaid."
        if drafts:
            text += f" {len(drafts)} draft(s) haven't been sent."
        blocks = [{"type": "text", "text": text},
                  {"type": "table", "title": "Unpaid", "columns": ["Who", "Amount", "Type", "Status", "ID"], "rows": rows}]
        if invoices:
            blocks.append({"type": "suggest", "text": "Remind everyone unpaid"})
        return blocks

    def _do_create_checkout_link(self, a: dict) -> list[dict]:
        currency = self._currency(a.get("currency"))
        items = []
        for it in a.get("items") or []:
            qty = int(it.get("quantity") or 1)
            if qty < 1 or qty > 1000:
                raise AgentError(f"Quantity {qty} is out of range.")
            items.append({"name": (it.get("name") or "Item").strip(), "quantity": qty,
                          "unit_amount": self._amount(it.get("unit_amount"), "price")})
        if not items:
            raise AgentError("What items should the checkout link include?")
        total = sum(i["unit_amount"] * i["quantity"] for i in items)
        self._amount(total, "order total")
        order = _public(self.client.create_order(items, currency))
        order["refunded"] = "0.00"
        self.orders[order["id"]] = order
        desc = ", ".join(f"{i['quantity']} × {i['name']} @ {fmt(i['unit_amount'], currency)}" for i in items)
        self._log(f"Checkout link {order['id']} ({fmt(order['amount'], currency)})")
        return [{"type": "text", "text": f"Checkout link ready for {desc}. Total {fmt(order['amount'], currency)}."},
                {"type": "link", "label": "Open PayPal checkout", "url": order["approve_url"],
                 "note": "Simulated checkout (mock mode)" if getattr(self.client, "is_mock", False)
                 else "Share this link with your buyer"}]

    def _do_capture_order(self, a: dict) -> list[dict]:
        order = self._resolve_order(a.get("order_ref", "LAST"), {"APPROVED", "CREATED"})
        if not getattr(self.client, "is_mock", False):
            order.update({k: v for k, v in _public(self.client.get_order(order["id"])).items() if v})
        if order["status"] == "COMPLETED":
            return [{"type": "text", "text": f"Order {order['id']} is already paid."}]
        if order["status"] != "APPROVED":
            return [{"type": "text", "text": f"Order {order['id']} hasn't been approved by the buyer yet "
                                             f"(status {order['status']})."}]
        return self._capture(order["id"])

    def _capture(self, order_id: str) -> list[dict]:
        captured = _public(self.client.capture_order(order_id))
        order = self.orders.setdefault(order_id, captured)
        order.update({k: v for k, v in captured.items() if v not in ("", None, [])})
        self._log(f"Captured {fmt(order['amount'], order['currency'])} for order {order_id}")
        return [{"type": "text", "text": f"Payment captured: {fmt(order['amount'], order['currency'])} for order "
                                         f"{order_id} (capture {order['capture_id']})."}]

    def on_buyer_approved(self, order_id: str) -> list[dict]:
        """Called when the buyer returns from PayPal checkout (or the mock checkout page)."""
        with self.lock:
            if order_id in self.orders:
                self.orders[order_id]["status"] = "APPROVED"
            self._ctx = {"tool": "capture_order", "approved_by": "buyer (approved on PayPal checkout)",
                         "why": "The buyer approved this order on PayPal, so capturing completes the payment they agreed to."}
            try:
                return self._capture(order_id)
            finally:
                self._ctx = {}
                self._persist()

    # ------------------------------------------------------------- gated actions (propose -> exec)
    def _propose_send_invoice(self, a: dict, invoice: dict | None = None, why: str = "") -> list[dict]:
        inv = invoice or self._resolve_invoice(a.get("invoice_ref"), {"DRAFT"})
        if inv["status"] != "DRAFT":
            raise AgentError(f"Invoice {inv['id']} is already {inv['status']}.")
        delivery = (f"PayPal emails {inv['recipient_email']}" if inv.get("recipient_email")
                    else "No email given: PayPal creates a pay link you can share (e.g. on WhatsApp)")
        details = [["Customer", inv["recipient_name"] or "—"], ["Amount", fmt(inv["amount"], inv["currency"])],
                   ["For", inv.get("description") or "—"], ["Due", inv.get("due_date") or "on receipt"],
                   ["Delivery", delivery], ["Invoice", inv["id"]]]
        return self._new_pending("send_invoice", {"invoice_id": inv["id"], "notify": bool(inv.get("recipient_email"))},
                                 f"Send invoice to {inv['recipient_name'] or 'customer'}?", details, why=why,
                                 headline={"action": "Send invoice", "amount": fmt(inv["amount"], inv["currency"]),
                                           "recipient": inv.get("recipient_email") or inv["recipient_name"] or "customer"})

    def _exec_send_invoice(self, a: dict) -> list[dict]:
        inv = self.client.send_invoice(a["invoice_id"], notify_recipient=a.get("notify", True))
        self._log(f"Sent invoice {inv['id']} to {inv['recipient_name']}")
        blocks = [{"type": "text", "text": f"Sent. Invoice {inv['id']} is now {inv['status']} "
                                           f"({fmt(inv['amount'], inv['currency'])})."}]
        if inv.get("pay_url"):
            blocks.append({"type": "link", "label": "Invoice pay link", "url": inv["pay_url"],
                           "note": "Simulated invoice page (mock mode)" if getattr(self.client, "is_mock", False)
                           else "Customer's PayPal invoice page"})
        return blocks

    def _propose_remind_invoice(self, a: dict) -> list[dict]:
        ref = a.get("invoice_ref", "")
        if ref.upper() == "ALL_UNPAID":
            invs = [i for i in self.client.list_invoices() if i["status"] in UNPAID_INVOICE_STATUSES]
            if not invs:
                return [{"type": "text", "text": "No unpaid invoices to remind."}]
        elif ref.upper() == "ALL_OVERDUE":
            invs = [i for i in self.client.list_invoices() if self._is_overdue(i)]
            if not invs:
                return [{"type": "text", "text": "Nobody is overdue right now, so there's no one to remind."}]
        else:
            invs = [self._resolve_invoice(ref, UNPAID_INVOICE_STATUSES)]
            if invs[0]["status"] not in UNPAID_INVOICE_STATUSES:
                raise AgentError(f"Invoice {invs[0]['id']} is {invs[0]['status']}, so there's nothing to remind about.")
        details = [[i["recipient_name"] or i["id"], f"{fmt(i.get('due_amount') or i['amount'], i['currency'])} · {i['id']}"]
                   for i in invs]
        names = [i["recipient_name"] or i["id"] for i in invs]
        distinct = list(dict.fromkeys(names))
        who = people_text(names) if len(distinct) == 1 else f"{len(distinct)} customers"
        if len(distinct) > 1 and len(invs) != len(distinct):
            who += f" ({len(invs)} invoices)"
        if ref.upper() == "ALL_OVERDUE":
            who += " (overdue)"
        return self._new_pending("remind_invoice", {"invoice_ids": [i["id"] for i in invs], "note": a.get("note", "")},
                                 f"Send a payment reminder to {who}?", details,
                                 headline={"action": "Payment reminder" + ("s" if len(invs) > 1 else ""),
                                           "amount": self._sum_text(invs),
                                           "recipient": people_text(names)})

    def _exec_remind_invoice(self, a: dict) -> list[dict]:
        done, skipped = [], []
        for inv_id in a["invoice_ids"]:
            try:
                inv = self.client.remind_invoice(inv_id, a.get("note", ""))
            except PayPalError as e:
                if len(a["invoice_ids"]) == 1:
                    raise
                skipped.append(f"{inv_id} ({getattr(e, 'friendly', '') or e})")
                self._log(f"Reminder skipped for {inv_id}: {e}")
                continue
            done.append(inv.get("recipient_name") or inv_id)
            self._log(f"Reminder sent for {inv_id}")
        blocks = []
        if done:
            plural = "s" if len(done) > 1 else ""
            blocks.append({"type": "text", "text": f"Reminder{plural} sent to {people_text(done)}."})
        if skipped:
            blocks.append({"type": "text", "text": "Skipped (PayPal can only remind invoices that have a customer email): "
                           + "; ".join(skipped)})
        return blocks or [{"type": "text", "text": "No reminders were sent."}]

    def _propose_cancel_invoice(self, a: dict) -> list[dict]:
        inv = self._resolve_invoice(a.get("invoice_ref"), UNPAID_INVOICE_STATUSES)
        if inv["status"] not in UNPAID_INVOICE_STATUSES:
            raise AgentError(f"Invoice {inv['id']} is {inv['status']}; only sent, unpaid invoices can be cancelled.")
        details = [["Customer", inv["recipient_name"]], ["Amount", fmt(inv["amount"], inv["currency"])],
                   ["Invoice", inv["id"]]]
        return self._new_pending("cancel_invoice", {"invoice_id": inv["id"]},
                                 f"Cancel {inv['recipient_name']}'s invoice?", details,
                                 headline={"action": "Cancel invoice", "amount": fmt(inv["amount"], inv["currency"]),
                                           "recipient": inv["recipient_name"] or inv["id"]})

    def _exec_cancel_invoice(self, a: dict) -> list[dict]:
        inv = self.client.cancel_invoice(a["invoice_id"])
        self._log(f"Cancelled invoice {inv['id']}")
        return [{"type": "text", "text": f"Invoice {inv['id']} cancelled; the customer has been notified."}]

    def _propose_refund_order(self, a: dict) -> list[dict]:
        order = self._resolve_order(a.get("order_ref", "LAST"), {"COMPLETED"})
        if order["status"] != "COMPLETED" or not order.get("capture_id"):
            raise AgentError(f"Order {order['id']} hasn't been paid (status {order['status']}), so there's nothing to refund.")
        remaining = Decimal(order["amount"]) - Decimal(order.get("refunded") or "0")
        if remaining <= 0:
            raise AgentError(f"Order {order['id']} has already been fully refunded.")
        amount = self._amount(a["amount"]) if a.get("amount") not in (None, "") else remaining
        if amount > remaining:
            raise AgentError(f"You can refund at most {fmt(remaining, order['currency'])} on order {order['id']}.")
        kind = "Full" if amount == remaining and Decimal(order.get("refunded") or "0") == 0 else "Partial"
        details = [["Order", order["id"]], ["Paid by", order.get("payer_name") or "buyer"],
                   ["Refund", f"{fmt(amount, order['currency'])} ({kind.lower()})"],
                   ["Order total", fmt(order["amount"], order["currency"])], ["Capture", order["capture_id"]]]
        return self._new_pending("refund_order", {"order_id": order["id"], "amount": str(amount)},
                                 f"{kind} refund of {fmt(amount, order['currency'])}?", details,
                                 headline={"action": f"{kind} refund", "amount": fmt(amount, order["currency"]),
                                           "recipient": order.get("payer_email") or order.get("payer_name") or "buyer"})

    def _exec_refund_order(self, a: dict) -> list[dict]:
        order = self.orders[a["order_id"]]
        amount = Decimal(a["amount"])
        refund = self.client.refund_capture(order["capture_id"], amount, order["currency"])
        order["refunded"] = str(Decimal(order.get("refunded") or "0") + amount)
        order["local_status"] = ("REFUNDED" if Decimal(order["refunded"]) >= Decimal(order["amount"])
                                 else "PARTIALLY_REFUNDED")
        self._log(f"Refunded {fmt(amount, order['currency'])} on {order['id']}")
        return [{"type": "text", "text": f"Refund {refund['id']} {refund['status'].lower()}: "
                                         f"{fmt(amount, order['currency'])} back to the buyer for order {order['id']}."}]

    def _propose_create_payout(self, a: dict) -> list[dict]:
        currency = self._currency(a.get("currency"))
        items = []
        for it in a.get("items") or []:
            email = self._email(it.get("receiver_email"))
            if not email:
                raise AgentError("Each payout needs the collaborator's PayPal email.")
            items.append({"receiver_email": email, "amount": str(self._amount(it.get("amount"))),
                          "note": it.get("note") or "Payout via AgentPay"})
        if not items:
            raise AgentError("Who should I pay, and how much?")
        total = sum(Decimal(i["amount"]) for i in items)
        details = [[i["receiver_email"], fmt(i["amount"], currency)] for i in items] + [["Total", fmt(total, currency)]]
        return self._new_pending("create_payout", {"items": items, "currency": currency},
                                 f"Pay out {fmt(total, currency)} to {len(items)} collaborator(s)?", details,
                                 headline={"action": "Payout", "amount": fmt(total, currency),
                                           "recipient": ", ".join(i["receiver_email"] for i in items)})

    @staticmethod
    def _sum_text(invs: list[dict]) -> str:
        totals: dict[str, Decimal] = {}
        for i in invs:
            totals[i["currency"]] = totals.get(i["currency"], Decimal("0")) + Decimal(i.get("due_amount") or i["amount"] or "0")
        return " + ".join(fmt(v, k) for k, v in totals.items())

    def _exec_create_payout(self, a: dict) -> list[dict]:
        res = self.client.create_payout(a["items"], a["currency"])
        self._log(f"Payout batch {res['batch_id']} ({res['status']})")
        return [{"type": "text", "text": f"Payout batch {res['batch_id']} submitted (status {res['status']})."}]

    # ------------------------------------------------------------- ledger for the side panel
    def _is_overdue(self, inv: dict) -> bool:
        if inv.get("status") not in UNPAID_INVOICE_STATUSES or not inv.get("due_date"):
            return False
        try:
            return date.fromisoformat(inv["due_date"][:10]) < self.today_fn()
        except ValueError:
            return False

    def invoice_display_status(self, inv: dict) -> str:
        return "OVERDUE" if self._is_overdue(inv) else inv.get("status", "")

    def ledger(self) -> dict:
        with self.lock:
            invoices = [{**_public(i), "display_status": self.invoice_display_status(i)}
                        for i in self.client.list_invoices()]
            if not getattr(self.client, "is_mock", False):
                changed = False
                for o in self.orders.values():
                    if o["status"] in {"CREATED", "APPROVED", "PAYER_ACTION_REQUIRED"}:
                        try:
                            fresh = _public(self.client.get_order(o["id"]))
                            if fresh["status"] and fresh["status"] != o["status"]:
                                o["status"], changed = fresh["status"], True
                        except PayPalError:
                            pass
                if changed:
                    self._persist()
            orders = [{**o, "display_status": o.get("local_status") or o["status"]} for o in self.orders.values()]
            totals: dict[str, Decimal] = {}
            for i in invoices:
                if i["status"] in UNPAID_INVOICE_STATUSES:
                    owed = Decimal(i.get("due_amount") or i.get("amount") or "0")
                    totals[i["currency"]] = totals.get(i["currency"], Decimal("0")) + owed
            return {"invoices": list(reversed(invoices)), "orders": list(reversed(orders)),
                    "activity": list(reversed(self.activity[-20:])),
                    "pending": [p.card() for p in self.pending.values()],
                    "owed": {k: str(v.quantize(Decimal("0.01"))) for k, v in totals.items()},
                    "counts": {"overdue": sum(1 for i in invoices if i["display_status"] == "OVERDUE"),
                               "awaiting_approval": len(self.pending)}}

    def audit(self, limit: int = 200) -> list[dict]:
        return self.store.audit(limit)

    # ------------------------------------------------------------- demo data (mock mode only)
    def seed_demo(self) -> dict:
        """Create a small, clearly-fake starting ledger so the overdue/remind/refund flows can be shown."""
        if not getattr(self.client, "is_mock", False):
            raise AgentError("Demo data is only available in mock mode.")
        with self.lock:
            today = self.today_fn()
            self._ctx = {"approved_by": "demo seed (mock mode)"}
            try:
                c = self.client
                meera = c.create_draft_invoice("Meera Nair", Decimal("1200"), "INR", "Diwali hamper order",
                                               "meera@example.com", (today - timedelta(days=9)).isoformat())
                c.send_invoice(meera["id"])
                kabir = c.create_draft_invoice("Kabir Shah", Decimal("60"), "USD", "Portrait sketch",
                                               "kabir@example.com", (today - timedelta(days=3)).isoformat())
                c.send_invoice(kabir["id"])
                ana = c.create_draft_invoice("Ana Lopez", Decimal("35"), "USD", "Sticker pack", "",
                                             (today - timedelta(days=5)).isoformat())
                c.send_invoice(ana["id"])
                c.simulate_invoice_payment(ana["id"])
                order = _public(c.create_order([{"name": "Tote bag", "quantity": 2, "unit_amount": Decimal("18")}], "USD"))
                order["refunded"] = "0.00"
                self.orders[order["id"]] = order
                c.simulate_buyer_approval(order["id"])
                captured = _public(c.capture_order(order["id"]))
                self.orders[order["id"]].update({k: v for k, v in captured.items() if v not in ("", None, [])})
                self._log("Loaded demo data: 2 overdue invoices, 1 paid invoice, 1 paid order (mock mode)",
                          actor="system")
            finally:
                self._ctx = {}
            self._persist()
            return self.ledger()
