#!/usr/bin/env bash
# End-to-end smoke test against a running app in MOCK mode.
# Usage: MOCK_MODE=1 uvicorn app.main:app --port 8000 &   then   ./scripts/smoke_mock.sh [base_url]
set -euo pipefail
B="${1:-http://127.0.0.1:8000}"
chat() { curl -s -X POST "$B/api/chat" -H 'content-type: application/json' -d "$(python3 -c 'import json,sys;print(json.dumps({"message":sys.argv[1]}))' "$1")"; }
card() { python3 -c 'import sys,json;print([b for b in json.load(sys.stdin)["blocks"] if b["type"]=="confirm"][0]["action_id"])'; }
link() { python3 -c 'import sys,json;print([b for b in json.load(sys.stdin)["blocks"] if b["type"]=="link"][0]["url"])'; }
approve() { curl -s -X POST "$B/api/confirm" -H 'content-type: application/json' -d "{\"action_id\":\"$1\",\"approve\":true}"; echo; }
step() { echo; echo "== $*"; }

step "status";                       curl -s "$B/api/status"; echo
step "create invoice (draft + approval card)"
R=$(chat 'Invoice Priya $40 for logo design, due Friday'); echo "$R"
step "approve send";                 approve "$(echo "$R" | card)"
step "second invoice, overdue";      approve "$(chat 'Invoice Arjun (arjun@example.com) ₹2,500 for website fixes, due 2026-11-01' | card)"
step "checkout link"
R=$(chat 'Make a checkout link for 2 T-shirts at $15'); echo "$R"
URL=$(echo "$R" | link); OID="${URL##*/}"
step "buyer pays on simulated checkout (auto-capture)"
curl -s -X POST "$B/api/mock/checkout/$OID/pay"; echo
step "partial refund";               approve "$(chat "Refund \$5 of order $OID" | card)"
step "refund the rest";              approve "$(chat 'Refund the last order' | card)"
step "who hasn't paid?";             chat "Who hasn't paid?"; echo
step "remind everyone";              approve "$(chat 'Remind everyone unpaid' | card)"
step "cancel Arjun's invoice";       approve "$(chat "Cancel Arjun's invoice" | card)"
step "payout split";                 approve "$(chat 'Pay $20 to alex@example.com and $10 to sam@example.com' | card)"
step "ledger"
curl -s "$B/api/ledger" | python3 -c 'import sys,json;d=json.load(sys.stdin)
print("invoices:", [(i["recipient_name"], i["status"]) for i in d["invoices"]])
print("orders:", [(o["id"], o["display_status"], o["refunded"]) for o in d["orders"]])'
