"""PayPalClient tests against a mocked HTTP transport (no network, no credentials)."""
import json
from decimal import Decimal

import httpx
import pytest

from app.paypal_client import PayPalClient, PayPalError

BASE = "https://api-m.sandbox.paypal.com"


class FakePayPal:
    """Records requests and returns canned PayPal-shaped responses."""

    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.token_calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path, method = request.url.path, request.method
        if path == "/v1/oauth2/token":
            self.token_calls += 1
            assert request.headers["Authorization"].startswith("Basic ")
            return httpx.Response(200, json={"access_token": "A21-test", "expires_in": 32400})
        assert request.headers["Authorization"] == "Bearer A21-test"
        if method == "POST" and path == "/v2/invoicing/invoices":
            body = json.loads(request.content)
            return httpx.Response(201, json={
                "id": "INV2-AAAA-BBBB", "status": "DRAFT",
                "detail": {"currency_code": body["detail"]["currency_code"], "invoice_number": "0001",
                           "payment_term": body["detail"].get("payment_term", {})},
                "primary_recipients": body["primary_recipients"], "items": body["items"],
                "amount": {"currency_code": "USD", "value": body["items"][0]["unit_amount"]["value"]},
            })
        if method == "POST" and path.endswith("/send"):
            return httpx.Response(200, json={"links": []})
        if method == "GET" and path.startswith("/v2/invoicing/invoices/"):
            return httpx.Response(200, json={
                "id": path.split("/")[-1], "status": "SENT",
                "detail": {"currency_code": "USD", "invoice_number": "0001",
                           "metadata": {"recipient_view_url": "https://www.sandbox.paypal.com/invoice/p/#X"}},
                "primary_recipients": [{"billing_info": {"name": {"given_name": "Priya"},
                                                         "email_address": "priya@example.com"}}],
                "items": [{"name": "Logo design"}], "amount": {"currency_code": "USD", "value": "40.00"},
                "due_amount": {"currency_code": "USD", "value": "40.00"}})
        if method == "GET" and path == "/v2/invoicing/invoices":
            return httpx.Response(200, json={"items": [
                {"id": "INV2-1", "status": "SENT", "detail": {"currency_code": "USD", "payment_term": {"due_date": "2026-11-13"}},
                 "primary_recipients": [{"billing_info": {"name": {"given_name": "Priya", "surname": "S"}}}],
                 "amount": {"currency_code": "USD", "value": "40.00"}, "due_amount": {"currency_code": "USD", "value": "40.00"}},
                {"id": "INV2-2", "status": "PAID", "detail": {"currency_code": "INR"},
                 "amount": {"currency_code": "INR", "value": "2500.00"}}]})
        if method == "POST" and path.endswith("/remind"):
            return httpx.Response(204)
        if method == "POST" and path == "/v2/checkout/orders":
            body = json.loads(request.content)
            return httpx.Response(201, json={
                "id": "5O190127TN364715T", "status": "PAYER_ACTION_REQUIRED", "purchase_units": body["purchase_units"],
                "links": [{"rel": "self", "href": f"{BASE}/v2/checkout/orders/5O190127TN364715T"},
                          {"rel": "payer-action", "href": "https://www.sandbox.paypal.com/checkoutnow?token=5O190127TN364715T"}]})
        if method == "POST" and path.endswith("/capture"):
            return httpx.Response(201, json={
                "id": "5O190127TN364715T", "status": "COMPLETED",
                "payer": {"name": {"given_name": "John", "surname": "Doe"}, "email_address": "buyer@example.com"},
                "purchase_units": [{"payments": {"captures": [{"id": "3C679366HH908993F", "status": "COMPLETED"}]}}]})
        if method == "POST" and path == "/v2/payments/captures/3C679366HH908993F/refund":
            body = json.loads(request.content or b"{}")
            amt = body.get("amount", {"value": "30.00", "currency_code": "USD"})
            return httpx.Response(201, json={"id": "1JU08902781691411", "status": "COMPLETED", "amount": amt})
        if method == "POST" and path == "/v1/payments/payouts":
            return httpx.Response(201, json={"batch_header": {"payout_batch_id": "5UXD2E8A7EBQJ", "batch_status": "PENDING"}})
        if path == "/v2/checkout/orders/BAD":
            return httpx.Response(404, json={"name": "RESOURCE_NOT_FOUND", "message": "The specified resource does not exist."})
        return httpx.Response(500, json={"message": f"unexpected {method} {path}"})


@pytest.fixture
def fake():
    return FakePayPal()


@pytest.fixture
def client(fake):
    return PayPalClient("cid", "secret", "sandbox", http=httpx.Client(transport=httpx.MockTransport(fake)),
                        return_url="http://app/paypal/return", cancel_url="http://app/paypal/cancel")


def _body(req):
    return json.loads(req.content)


def test_token_is_fetched_once_and_cached(client, fake):
    client.list_invoices()
    client.list_invoices()
    assert fake.token_calls == 1


def test_create_draft_invoice_request_shape(client, fake):
    inv = client.create_draft_invoice("Priya Sharma", Decimal("40"), "usd", "Logo design",
                                      recipient_email="priya@example.com", due_date="2026-11-13")
    req = fake.requests[-1]
    body = _body(req)
    assert req.url.path == "/v2/invoicing/invoices"
    assert req.headers["Prefer"] == "return=representation"
    assert req.headers["PayPal-Request-Id"]  # idempotency key on money-related POSTs
    assert body["detail"] == {"currency_code": "USD", "payment_term": {"due_date": "2026-11-13"}}
    assert body["primary_recipients"][0]["billing_info"] == {
        "name": {"given_name": "Priya", "surname": "Sharma"}, "email_address": "priya@example.com"}
    assert body["items"][0] == {"name": "Logo design", "quantity": "1",
                                "unit_amount": {"currency_code": "USD", "value": "40.00"}}
    assert inv["id"] == "INV2-AAAA-BBBB" and inv["status"] == "DRAFT"
    assert inv["recipient_name"] == "Priya Sharma" and inv["amount"] == "40.00"


def test_send_invoice_then_returns_pay_link(client, fake):
    inv = client.send_invoice("INV2-AAAA-BBBB", notify_recipient=False)
    send_req = next(r for r in fake.requests if r.url.path.endswith("/send"))
    assert _body(send_req) == {"send_to_invoicer": False, "send_to_recipient": False}
    assert inv["status"] == "SENT"
    assert inv["pay_url"].startswith("https://www.sandbox.paypal.com/invoice/")


def test_list_invoices_normalized(client):
    invs = client.list_invoices()
    assert [i["id"] for i in invs] == ["INV2-1", "INV2-2"]
    assert invs[0]["recipient_name"] == "Priya S" and invs[0]["due_date"] == "2026-11-13"
    assert invs[1]["currency"] == "INR" and invs[1]["recipient_name"] == ""


def test_remind_invoice_204(client, fake):
    client.remind_invoice("INV2-1", "Please pay")
    req = next(r for r in fake.requests if r.url.path.endswith("/remind"))
    assert _body(req)["note"] == "Please pay" and _body(req)["send_to_recipient"] is True


def test_create_order_totals_and_approve_link(client, fake):
    order = client.create_order([{"name": "T-shirt", "quantity": 2, "unit_amount": Decimal("15")}], "USD")
    body = _body(fake.requests[-1])
    unit = body["purchase_units"][0]
    assert body["intent"] == "CAPTURE"
    assert unit["amount"] == {"currency_code": "USD", "value": "30.00",
                              "breakdown": {"item_total": {"currency_code": "USD", "value": "30.00"}}}
    assert unit["items"][0] == {"name": "T-shirt", "quantity": "2", "unit_amount": {"currency_code": "USD", "value": "15.00"}}
    assert body["payment_source"]["paypal"]["experience_context"]["return_url"] == "http://app/paypal/return"
    assert order["approve_url"].endswith("token=5O190127TN364715T")
    assert order["amount"] == "30.00" and order["items"][0]["quantity"] == 2


def test_capture_extracts_capture_id_and_payer(client):
    order = client.capture_order("5O190127TN364715T")
    assert order["status"] == "COMPLETED"
    assert order["capture_id"] == "3C679366HH908993F"
    assert order["payer_name"] == "John Doe"


def test_partial_and_full_refund(client, fake):
    partial = client.refund_capture("3C679366HH908993F", Decimal("5"), "USD")
    assert _body(fake.requests[-1]) == {"amount": {"currency_code": "USD", "value": "5.00"}}
    assert partial["status"] == "COMPLETED" and partial["amount"] == "5.00"
    client.refund_capture("3C679366HH908993F")
    assert _body(fake.requests[-1]) == {}  # empty body = full refund


def test_payout(client, fake):
    res = client.create_payout([{"receiver_email": "alex@example.com", "amount": "20"}], "USD")
    body = _body(fake.requests[-1])
    assert body["items"][0]["receiver"] == "alex@example.com"
    assert body["items"][0]["amount"] == {"value": "20.00", "currency": "USD"}
    assert res["batch_id"] == "5UXD2E8A7EBQJ" and res["status"] == "PENDING"


def test_errors_raise_paypal_error(client):
    with pytest.raises(PayPalError) as exc:
        client.get_order("BAD")
    assert exc.value.status_code == 404
    assert "does not exist" in str(exc.value)


def test_auth_failure():
    def handler(req):
        return httpx.Response(401, json={"error": "invalid_client", "error_description": "Client Authentication failed"})
    c = PayPalClient("x", "y", http=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(PayPalError) as exc:
        c.list_invoices()
    assert exc.value.status_code == 401


def test_rejects_unknown_env():
    with pytest.raises(ValueError):
        PayPalClient("x", "y", env="prod")
