"""Deterministic, rule-based intent parser.

Used in MOCK mode (or whenever no LLM key is configured) so the demo is fully reproducible
with zero credentials. It maps short seller commands to the same ToolCalls the LLM emits.
It is intentionally conservative: when it is not sure, it returns a help reply instead of
guessing an action.
"""
from __future__ import annotations

import calendar
import re
from datetime import date, timedelta
from decimal import Decimal

from .tools import Plan, ToolCall

SYMBOLS = {"$": "USD", "₹": "INR", "€": "EUR", "£": "GBP"}
WORD_CODES = {
    "usd": "USD", "dollar": "USD", "dollars": "USD", "inr": "INR", "rs": "INR", "rs.": "INR",
    "rupee": "INR", "rupees": "INR", "eur": "EUR", "euro": "EUR", "euros": "EUR", "gbp": "GBP",
    "pounds": "GBP", "aud": "AUD", "cad": "CAD", "sgd": "SGD", "jpy": "JPY",
}
_NUM = r"\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?"
_CODES = r"usd|inr|eur|gbp|aud|cad|sgd|jpy|rs\.?|dollars?|rupees?|euros?|pounds"
AMOUNT_RE = re.compile(
    rf"(?P<sym>[$₹€£])\s?(?P<v1>{_NUM})"
    rf"|\b(?P<c1>{_CODES})\s?(?P<v2>{_NUM})\b"
    rf"|\b(?P<v3>{_NUM})\s?(?P<c2>{_CODES})(?![a-z])",
    re.IGNORECASE,
)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
ID_RE = re.compile(r"\b(?=[A-Z0-9-]*\d)[A-Z0-9][A-Z0-9-]{5,}\b", re.IGNORECASE)
WEEKDAYS = {name.lower(): i for i, name in enumerate(calendar.day_name)}
WEEKDAYS.update({name.lower(): i for i, name in enumerate(calendar.day_abbr)})
MONTHS = {name.lower(): i for i, name in enumerate(calendar.month_name) if name}
MONTHS.update({name.lower(): i for i, name in enumerate(calendar.month_abbr) if name})

HELP = (
    "I can help with PayPal invoices, checkout links, refunds and payouts. Try:\n"
    "• Invoice Priya $40 for logo design, due Friday\n"
    "• Make a checkout link for 2 T-shirts at $15\n"
    "• Who hasn't paid?\n"
    "• Remind Priya  •  Cancel Priya's invoice\n"
    "• Refund the last order  •  Refund $5 of order ORDER-MOCK-0002\n"
    "• Pay $20 to alex@example.com and $10 to sam@example.com"
)


# ------------------------------------------------------------------ helpers

def find_amount(text: str, default_currency: str = "USD"):
    """Return (Decimal amount, currency, match) for the first money expression, or None."""
    m = AMOUNT_RE.search(text)
    if not m:
        return None
    if m.group("sym"):
        value, cur = m.group("v1"), SYMBOLS[m.group("sym")]
    elif m.group("c1"):
        value, cur = m.group("v2"), WORD_CODES[m.group("c1").lower()]
    else:
        value, cur = m.group("v3"), WORD_CODES[m.group("c2").lower()]
    return Decimal(value.replace(",", "")), cur, m


def find_all_amounts(text: str):
    return [m for m in AMOUNT_RE.finditer(text)]


def _amount_from_match(m) -> tuple[Decimal, str]:
    if m.group("sym"):
        return Decimal(m.group("v1").replace(",", "")), SYMBOLS[m.group("sym")]
    if m.group("c1"):
        return Decimal(m.group("v2").replace(",", "")), WORD_CODES[m.group("c1").lower()]
    return Decimal(m.group("v3").replace(",", "")), WORD_CODES[m.group("c2").lower()]


def parse_due_date(phrase: str, today: date) -> str:
    """Resolve 'Friday', 'tomorrow', 'in 10 days', 'next week', '2026-11-20', 'Nov 20', '20 Nov'."""
    p = phrase.strip().lower().rstrip(".!")
    p = re.sub(r"^(on|by|this)\s+", "", p)
    if p in {"today", "now"}:
        return today.isoformat()
    if p == "tomorrow":
        return (today + timedelta(days=1)).isoformat()
    if p in {"next week", "in a week"}:
        return (today + timedelta(days=7)).isoformat()
    if p in {"end of month", "end of the month", "month end"}:
        last = calendar.monthrange(today.year, today.month)[1]
        return today.replace(day=last).isoformat()
    m = re.fullmatch(r"in\s+(\d+)\s+(day|days|week|weeks)", p)
    if m:
        n = int(m.group(1)) * (7 if m.group(2).startswith("week") else 1)
        return (today + timedelta(days=n)).isoformat()
    m = re.fullmatch(r"(next\s+)?([a-z]+)", p)
    if m and m.group(2) in WEEKDAYS:
        if m.group(1):  # "next friday" = that weekday in the following Mon-Sun week
            next_monday = today + timedelta(days=7 - today.weekday())
            return (next_monday + timedelta(days=WEEKDAYS[m.group(2)])).isoformat()
        delta = (WEEKDAYS[m.group(2)] - today.weekday()) % 7 or 7  # upcoming, never today
        return (today + timedelta(days=delta)).isoformat()
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", p)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
    m = re.fullmatch(r"([a-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?", p) or None
    day_month = None
    if m and m.group(1) in MONTHS:
        day_month = (int(m.group(2)), MONTHS[m.group(1)])
    m2 = re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]+)", p)
    if m2 and m2.group(2) in MONTHS:
        day_month = (int(m2.group(1)), MONTHS[m2.group(2)])
    if day_month:
        d = date(today.year, day_month[1], day_month[0])
        if d < today:
            d = date(today.year + 1, day_month[1], day_month[0])
        return d.isoformat()
    return ""


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip(" ,.;:-")


def _name_ref(text: str) -> str:
    """Pull a customer name / id reference out of a short command remainder."""
    m = ID_RE.search(text)
    if m:
        return m.group(0).upper()
    m = re.search(r"\b([A-Za-z][\w]*)'s\b", text)
    if m:
        return m.group(1)
    words = [w for w in re.findall(r"[A-Za-z][\w-]*", text)
             if w.lower() not in {"the", "a", "an", "invoice", "invoices", "for", "to", "please", "my", "her",
                                  "his", "their", "about", "of", "order", "payment", "send", "reminder", "on", "it"}]
    return " ".join(words[:2])


# ------------------------------------------------------------------ parser

_VERBS = r"(?:remind|nudge|follow up|invoice|bill|charge|make|create|refund|cancel|void|pay|send|who|list|show|capture)"
CLAUSE_SPLIT = re.compile(
    rf"\s*[,;]\s*(?:and\s+)?(?:then\s+)?(?={_VERBS}\b)"      # "…, remind anyone overdue"
    rf"|\.\s+(?={_VERBS}\b)"                                   # "…. Then remind"
    rf"|\s+(?:and\s+)?then\s+(?={_VERBS}\b)"                   # "… then refund …"
    rf"|\s+and\s+(?=(?:remind|nudge|refund|cancel|who|list|show)\b)",  # "… and remind …"
    re.IGNORECASE,
)


def split_clauses(text: str) -> list[str]:
    """Split a multi-part request into clauses that each start with an action verb."""
    return [c.strip() for c in CLAUSE_SPLIT.split(text) if c and c.strip()]


def parse(text: str, today: date | None = None, default_currency: str = "USD") -> Plan:
    """Parse one request, which may contain several actions ("Invoice A $5 and B $9, remind anyone overdue")."""
    clauses = split_clauses(text or "")
    if len(clauses) <= 1:
        return parse_one(text, today, default_currency)
    calls, notes = [], []
    for clause in clauses:
        sub = parse_one(clause, today, default_currency)
        if sub.tool_calls:
            calls += sub.tool_calls
        else:
            notes.append(f"I skipped “{clause}”: {sub.reply.splitlines()[0] if sub.reply else 'not understood'}")
    if not calls:
        return parse_one(text, today, default_currency)
    return Plan(calls, " ".join(notes))


_LIST_SPLIT = re.compile(r"\s*(?:,(?!\d{3})\s*(?:and\s+)?|\s+and\s+|\s*&\s*)\s*", re.IGNORECASE)
_NAME_RE = re.compile(r"[A-Za-z][\w .'-]*")


def _recipient(seg: str) -> tuple[str, str] | None:
    """'Meera (meera@x.com)' / 'Meera <meera@x.com>' / 'Meera meera@x.com' / 'Meera' -> (name, email)."""
    email = ""
    e = EMAIL_RE.search(seg)
    if e:
        email = e.group(0)
        seg = seg[: e.start()] + seg[e.end():]
        seg = re.sub(r"\(\s*\)|<\s*>|\[\s*\]", " ", seg)
        seg = re.sub(r"(?i)\s+(?:at|email|e-mail)\s*:?\s*$", " ", seg)
    name = _clean(re.sub(r"(?i)^\s*(?:to|and)\s+|\s+for\s*$", " ", seg))
    if not name or not _NAME_RE.fullmatch(name) or EMAIL_RE.search(name):
        return None
    return name, email


def _multi_invoice(rest: str, base_args: dict) -> list[ToolCall]:
    """Several recipients in one request -> one create_invoice per person.

    Handles comma / 'and' / '&' lists where each recipient has an optional (email) and an amount:
      'Meera (meera@x.com) $60 and Kabir (kabir@x.com) $35 for the workshop'
      'Priya $40, Arjun $25 & Sam $10 for the workshop'
      'Meera $60 for design and Kabir $35 for the workshop'   (per-person descriptions)
      'Meera and Kabir $35 each for the workshop'             (one amount for everyone)
    The trailing 'for X' is shared by everyone who has no description of their own. The due date
    (already pulled out by the caller into base_args) applies to every invoice.
    """
    ms = list(AMOUNT_RE.finditer(EMAIL_RE.sub(lambda m: "_" * len(m.group(0)), rest)))
    if not ms:
        return []
    # "A and B $35 each for X": one amount shared by every named person
    if len(ms) == 1:
        m = ms[0]
        each = re.match(r"(?i)\s*(?:each|apiece|per (?:person|head))\b", rest[m.end():])
        if not each:
            return []
        people = [_recipient(s) for s in _LIST_SPLIT.split(rest[: m.start()]) if s.strip()]
        if len(people) < 2 or any(p is None for p in people):
            return []
        amount, cur = _amount_from_match(m)
        desc = _clean(re.sub(r"(?i)^\s*(for|-|:)\s*", "", rest[m.end() + each.end():])) or "Services"
        return [ToolCall("create_invoice", {**base_args, "recipient_name": n, "amount": float(amount),
                                            "currency": cur, "description": desc,
                                            **({"recipient_email": e} if e else {})}) for n, e in people]
    # Split the list into segments; each must hold exactly one amount. Commas inside an amount
    # ("₹2,500") and the shared tail ("for the workshop") are handled by the split pattern / below.
    entries = []  # (name, email, amount, currency, own_desc)
    for seg in _LIST_SPLIT.split(rest):
        if not seg.strip():
            continue
        masked = EMAIL_RE.sub(lambda m: "_" * len(m.group(0)), seg)
        seg_ms = list(AMOUNT_RE.finditer(masked))
        if len(seg_ms) != 1:
            if not seg_ms and entries:   # "…for paint, brushes and canvas": part of the last description
                n, e, amt, cur, d = entries[-1]
                entries[-1] = (n, e, amt, cur, f"{d}, {_clean(seg)}" if d else _clean(seg))
                continue
            return []
        m = seg_ms[0]
        person = _recipient(seg[: m.start()])
        if person is None:
            return []
        amount, cur = _amount_from_match(m)
        own = _clean(re.sub(r"(?i)^\s*(for|-|:)\s*", "", seg[m.end():]))
        entries.append((person[0], person[1], amount, cur, own))
    if len(entries) < 2:
        return []
    shared = entries[-1][4] or "Services"
    calls = []
    for name, email, amount, cur, own in entries:
        args = {**base_args, "recipient_name": name, "amount": float(amount), "currency": cur,
                "description": own or shared}
        if email:
            args["recipient_email"] = email
        calls.append(ToolCall("create_invoice", args))
    return calls


def parse_one(text: str, today: date | None = None, default_currency: str = "USD") -> Plan:
    today = today or date.today()
    raw = text.strip()
    low = raw.lower()
    if not raw:
        return Plan(reply=HELP)

    # Reminders
    if re.search(r"\bremind|\breminder|\bnudge|\bfollow up\b", low):
        if re.search(r"\b(everyone|everybody|all|anyone|anybody|whoever)\b", low):
            ref = "ALL_OVERDUE" if re.search(r"\b(overdue|late|past due)\b", low) else "ALL_UNPAID"
            return Plan([ToolCall("remind_invoice", {"invoice_ref": ref})])
        rest = re.sub(r"(?i)\b(send|a|remind(er)?|nudge|follow up( with)?)\b", " ", raw)
        ref = _name_ref(rest)
        if not ref:
            return Plan(reply="Who should I remind? Say a customer name, an invoice ID, or 'everyone'.")
        return Plan([ToolCall("remind_invoice", {"invoice_ref": ref})])

    # Who hasn't paid / unpaid summary
    if re.search(r"who (hasn'?t|has not|haven'?t|have not|didn'?t|did not) paid|who owes|\bunpaid\b|"
                 r"\boutstanding\b|\boverdue\b|pending payments?|owes? me", low):
        return Plan([ToolCall("unpaid_summary")])

    # List invoices
    m = re.search(r"\b(list|show|see|view)\b.*\binvoices?\b|^invoices$", low)
    if m and not re.search(r"\bremind|\bcancel|\brefund", low):
        status = next((s.upper() for s in ["draft", "sent", "paid", "cancelled"] if re.search(rf"\b{s}\b", low)), "")
        return Plan([ToolCall("list_invoices", {"status": status} if status else {})])

    # Refunds
    if re.search(r"\brefund", low):
        args: dict = {}
        amt = find_amount(raw, default_currency)
        rest = raw
        if amt:
            args["amount"] = float(amt[0])
            rest = raw[: amt[2].start()] + raw[amt[2].end():]
        if re.search(r"\b(last|latest|recent|previous)\b", rest.lower()):
            args["order_ref"] = "LAST"
        else:
            rest = re.sub(r"(?i)\brefund(ed)?\b|\bpartial(ly)?\b|\bfull(y)?\b", " ", rest)
            ref = _name_ref(rest)
            if not ref:
                return Plan(reply="Which order should I refund? Give me the order ID or say 'the last order'.")
            args["order_ref"] = ref
        return Plan([ToolCall("refund_order", args)])

    # Cancel invoice
    if re.search(r"\b(cancel|void)\b", low):
        rest = re.sub(r"(?i)\b(cancel|void)\b", " ", raw)
        ref = _name_ref(rest)
        if not ref:
            return Plan(reply="Which invoice should I cancel?")
        return Plan([ToolCall("cancel_invoice", {"invoice_ref": ref})])

    # Capture an approved order
    if re.search(r"\bcapture\b", low):
        rest = re.sub(r"(?i)\bcapture\b", " ", raw)
        ref = "LAST" if re.search(r"\b(last|latest)\b", low) else _name_ref(rest)
        return Plan([ToolCall("capture_order", {"order_ref": ref or "LAST"})])

    # Payouts / splits
    emails = EMAIL_RE.findall(raw)
    if emails and re.search(r"\b(pay ?out|payouts?|split|pay|send)\b", low) and not re.search(r"\binvoice\b", low):
        items = []
        for seg in re.split(r"\band\b|,|;", raw):
            e = EMAIL_RE.search(seg)
            a = find_amount(seg, default_currency)
            if e and a:
                items.append({"receiver_email": e.group(0), "amount": float(a[0]), "_cur": a[1]})
        if items:
            currency = items[0].pop("_cur")
            for i in items[1:]:
                i.pop("_cur", None)
            return Plan([ToolCall("create_payout", {"items": items, "currency": currency})])

    # Checkout links
    if re.search(r"\b(checkout|payment link|pay link|pay-link|buy link|link)\b", low):
        body = re.split(r"(?i)\bfor\b", raw, maxsplit=1)
        body = body[1] if len(body) > 1 else raw
        items, currency = [], default_currency
        item_re = re.compile(rf"^\s*(?:(\d+)\s*x?\s+)?(.+?)\s+(?:at|@|for|each at)\s+(.+?)(?:\s+each)?\s*$", re.I)
        for seg in re.split(r",|\band\b|\+", body):
            if not seg.strip():
                continue
            m = item_re.match(seg.strip())
            if not m:
                continue
            a = find_amount(m.group(3), default_currency)
            if not a:
                continue
            currency = a[1]
            items.append({"name": _clean(m.group(2)), "quantity": int(m.group(1) or 1), "unit_amount": float(a[0])})
        if items:
            return Plan([ToolCall("create_checkout_link", {"items": items, "currency": currency})])
        return Plan(reply="What should the checkout link be for? e.g. 'checkout link for 2 T-shirts at $15'.")

    # Send an existing draft
    m = re.match(r"(?i)^\s*send\s+(?:the\s+)?(?:draft\s+)?invoice\b(.*)$", raw)
    if m and not find_amount(raw):
        ref = _name_ref(m.group(1))
        if ref:
            return Plan([ToolCall("send_invoice", {"invoice_ref": ref})])

    # Create invoice
    m = re.match(r"(?i)^\s*(?:please\s+)?(?:create\s+(?:an?\s+)?|send\s+(?:an?\s+)?|make\s+(?:an?\s+)?)?"
                 r"(invoice|bill|charge)\b\s*(?:to\s+)?(.*)$", raw)
    if m:
        rest = m.group(2)
        args = {}
        dm = re.search(r"(?i)[,;]?\s*\b(?:due|payable by|by)\s+([^@]+)$", rest)
        if dm and parse_due_date(dm.group(1), today):
            args["due_date"] = parse_due_date(dm.group(1), today)
            rest = rest[: dm.start()]
        multi = _multi_invoice(rest, args)
        if multi:
            return Plan(multi)
        e = EMAIL_RE.search(rest)
        if e:
            args["recipient_email"] = e.group(0)
            rest = rest[: e.start()] + rest[e.end():]
            rest = re.sub(r"\(\s*\)|<\s*>", " ", rest)
        a = find_amount(rest, default_currency)
        if not a:
            return Plan(reply="How much should the invoice be for? e.g. 'Invoice Priya $40 for logo design'.")
        args["amount"], args["currency"] = float(a[0]), a[1]
        before, after = rest[: a[2].start()], rest[a[2].end():]
        before = re.sub(r"(?i)\s+for\s*$", "", before)
        name, desc = before, after
        if re.search(r"(?i)\sfor\s", " " + before + " "):  # "Invoice Priya for logo design $40"
            name, desc = re.split(r"(?i)\s+for\s+", before, maxsplit=1)
        desc = re.sub(r"(?i)^\s*(for|-|:)\s*", "", desc)
        name, desc = _clean(name), _clean(desc)
        if not name:
            return Plan(reply="Who is the invoice for?")
        args["recipient_name"] = name
        args["description"] = desc or "Services"
        return Plan([ToolCall("create_invoice", args)])

    return Plan(reply=HELP)
