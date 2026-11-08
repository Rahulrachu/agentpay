from datetime import date
from decimal import Decimal

import pytest

from app.intent_parser import find_amount, parse, parse_due_date

TODAY = date(2026, 11, 8)  # a Sunday


def one(text):
    plan = parse(text, today=TODAY)
    assert len(plan.tool_calls) == 1, plan.reply
    return plan.tool_calls[0]


def test_invoice_basic_with_weekday_due():
    c = one("Invoice Priya $40 for logo design, due Friday")
    assert c.name == "create_invoice"
    assert c.args == {"recipient_name": "Priya", "amount": 40.0, "currency": "USD",
                      "description": "logo design", "due_date": "2026-11-13"}


def test_invoice_description_before_amount():
    c = one("Invoice Priya for logo design $40")
    assert c.args["recipient_name"] == "Priya"
    assert c.args["description"] == "logo design"
    assert c.args["amount"] == 40.0


def test_invoice_inr_with_email_and_relative_due():
    c = one("invoice Rahul Kumar (rahul@example.com) ₹2,500 for website, due in 7 days")
    assert c.args["recipient_name"] == "Rahul Kumar"
    assert c.args["recipient_email"] == "rahul@example.com"
    assert c.args["amount"] == 2500.0 and c.args["currency"] == "INR"
    assert c.args["due_date"] == "2026-11-15"


def test_bill_synonym_with_currency_code_and_month_date():
    c = one("Bill Acme Corp 150 EUR - consulting by Nov 20")
    assert c.name == "create_invoice"
    assert c.args["currency"] == "EUR" and c.args["amount"] == 150.0
    assert c.args["recipient_name"] == "Acme Corp"
    assert c.args["due_date"] == "2026-11-20"


def test_invoice_without_amount_asks_instead_of_guessing():
    plan = parse("Invoice Priya due friday", today=TODAY)
    assert plan.tool_calls == []
    assert "How much" in plan.reply


def test_checkout_single_item():
    c = one("Make a checkout link for 2 T-shirts at $15")
    assert c.name == "create_checkout_link"
    assert c.args == {"items": [{"name": "T-shirts", "quantity": 2, "unit_amount": 15.0}], "currency": "USD"}


def test_checkout_multiple_items():
    c = one("payment link for 2 T-shirts at $15 and 1 mug at $8")
    assert [i["name"] for i in c.args["items"]] == ["T-shirts", "mug"]
    assert c.args["items"][1] == {"name": "mug", "quantity": 1, "unit_amount": 8.0}


@pytest.mark.parametrize("text,ref", [("Refund order X", "X"), ("refund the last order", "LAST"),
                                      ("Refund order ORDER-MOCK-0002", "ORDER-MOCK-0002")])
def test_refund_refs(text, ref):
    c = one(text)
    assert c.name == "refund_order" and c.args["order_ref"] == ref
    assert "amount" not in c.args


def test_partial_refund_amount():
    c = one("Refund $5 of order ORDER-MOCK-0002")
    assert c.args == {"order_ref": "ORDER-MOCK-0002", "amount": 5.0}


@pytest.mark.parametrize("text", ["Who hasn't paid?", "who owes me money", "show unpaid", "any overdue invoices?"])
def test_unpaid_summary(text):
    assert one(text).name == "unpaid_summary"


def test_reminders_and_cancel():
    assert one("remind Priya").args == {"invoice_ref": "Priya"}
    assert one("Remind everyone").args == {"invoice_ref": "ALL_UNPAID"}
    c = one("cancel Priya's invoice")
    assert c.name == "cancel_invoice" and c.args == {"invoice_ref": "Priya"}


def test_list_with_status():
    assert one("list paid invoices").args == {"status": "PAID"}
    assert one("show my invoices").args == {}


def test_payout_split():
    c = one("Pay $20 to alex@example.com and $10 to sam@example.com")
    assert c.name == "create_payout"
    assert c.args["items"] == [{"receiver_email": "alex@example.com", "amount": 20.0},
                               {"receiver_email": "sam@example.com", "amount": 10.0}]


def test_send_and_capture_by_id():
    assert one("send invoice INV2-MOCK-0001").args == {"invoice_ref": "INV2-MOCK-0001"}
    assert one("capture order ORDER-MOCK-0003").args == {"order_ref": "ORDER-MOCK-0003"}


def test_unknown_returns_help():
    plan = parse("what's the weather", today=TODAY)
    assert plan.tool_calls == [] and "Invoice Priya" in plan.reply


@pytest.mark.parametrize("phrase,expected", [
    ("Friday", "2026-11-13"), ("tomorrow", "2026-11-09"), ("next week", "2026-11-15"),
    ("next friday", "2026-11-13"), ("sunday", "2026-11-15"), ("2026-12-01", "2026-12-01"),
    ("20 Nov", "2026-11-20"), ("Jan 5", "2027-01-05"), ("end of month", "2026-11-30"), ("someday", ""),
])
def test_due_dates(phrase, expected):
    assert parse_due_date(phrase, TODAY) == expected


@pytest.mark.parametrize("text,amount,cur", [("$40", "40", "USD"), ("₹1,500.50", "1500.50", "INR"),
                                             ("Rs 999", "999", "INR"), ("25 euros", "25", "EUR"),
                                             ("GBP 12.5", "12.5", "GBP")])
def test_amounts(text, amount, cur):
    value, currency, _ = find_amount(text)
    assert value == Decimal(amount) and currency == cur


def test_next_weekday_from_midweek():
    # From Wednesday 11 Nov, "Friday" is this week and "next Friday" is the following week.
    wed = date(2026, 11, 11)
    assert parse_due_date("Friday", wed) == "2026-11-13"
    assert parse_due_date("next friday", wed) == "2026-11-20"


def test_remind_everyone_unpaid_is_a_reminder_not_a_summary():
    assert one("Remind everyone unpaid").args == {"invoice_ref": "ALL_UNPAID"}
