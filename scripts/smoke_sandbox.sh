#!/usr/bin/env bash
# Live smoke test against a running AgentPay server that uses REAL PayPal *sandbox* credentials.
#
#   uvicorn app.main:app --port 8000            # with PAYPAL_CLIENT_ID/SECRET set, PAYPAL_ENV=sandbox
#   ./scripts/smoke_sandbox.sh http://127.0.0.1:8000
#
# It approves its own confirmation cards through the API, so it really creates, sends, reminds and
# cancels sandbox invoices. Recipients are fake name.demo@example.com addresses (sandbox mail is not
# delivered). It never runs against PAYPAL_ENV=live. Steps whose result depends on the business
# account's country (payouts, INR) are reported as EXPECTED-BY-COUNTRY instead of failing the run.
set -uo pipefail
B="${1:-http://127.0.0.1:8000}"
B="${B%/}"
PASS=0; FAIL=0; SOFT=0
TAG="$(date +%H%M%S)"   # makes this run's invoices easy to spot in the sandbox dashboard

py() { python3 -c "$@"; }
post() { curl -sS -X POST "$B$1" -H 'content-type: application/json' -d "$2"; }
chat() { post /api/chat "$(py 'import json,sys;print(json.dumps({"message":sys.argv[1]}))' "$1")"; }
approve() { post /api/confirm "{\"action_id\":\"$1\",\"approve\":true}"; }
# first confirm card's action_id (top-level or inside a plan step)
card() { py 'import sys,json
bl=json.load(sys.stdin)["blocks"]
for b in bl:
    for x in ([b] + [y for s in b.get("steps",[]) for y in s.get("blocks",[])]):
        if x.get("type")=="confirm": print(x["action_id"]); sys.exit()'; }
plan_id() { py 'import sys,json;print(next((b["plan_id"] for b in json.load(sys.stdin)["blocks"] if b["type"]=="plan"),""))'; }
link() { py 'import sys,json;print(next((b["url"] for b in json.load(sys.stdin)["blocks"] if b["type"]=="link"),""))'; }
texts() { py 'import sys,json
d=json.load(sys.stdin)
for b in d.get("blocks",[]):
    if b["type"] in ("text","error"): print("   ", b["type"].upper()+":", b["text"])
    if b["type"]=="table": print("    TABLE", b.get("title"), "|", b["rows"])
    if b["type"]=="link": print("    LINK:", b["url"])'; }
errors() { py 'import sys,json
d=json.load(sys.stdin); bl=list(d.get("blocks",[]))
for b in list(bl): bl += [y for s in b.get("steps",[]) for y in s.get("blocks",[])]
print("\n".join(b["text"] for b in bl if b["type"]=="error"))'; }
ok()   { echo "  PASS: $*"; PASS=$((PASS+1)); }
bad()  { echo "  FAIL: $*"; FAIL=$((FAIL+1)); }
soft() { echo "  EXPECTED-BY-COUNTRY: $*"; SOFT=$((SOFT+1)); }
step() { echo; echo "== $*"; }
check() { # check <label> <json>   -> PASS if no error blocks
  local e; e=$(echo "$2" | errors); echo "$2" | texts
  if [ -z "$e" ]; then ok "$1"; else bad "$1: $e"; fi
}

step "server status"
S=$(curl -sS "$B/api/status") || { echo "Server not reachable at $B"; exit 2; }
echo "    $S"
MODE=$(echo "$S" | py 'import sys,json;d=json.load(sys.stdin)
print("mock" if d.get("paypal_mock") else ("live" if "live" in d.get("paypal","").lower() else "sandbox"))')
if [ "$MODE" = "live" ]; then echo "Refusing to run against PAYPAL_ENV=live."; exit 2; fi
if [ "$MODE" = "mock" ] && [ "${SMOKE_ALLOW_MOCK:-0}" != "1" ]; then
  echo "Server is in MOCK mode. Start it with sandbox keys (or set SMOKE_ALLOW_MOCK=1 to dry-run the script)."; exit 2
fi
echo "    mode: $MODE"

step "1. create + send one invoice (with email)"
R=$(chat "Invoice Priya (priya.demo@example.com) \$40 for logo design $TAG, due Friday"); echo "$R" | texts
A=$(echo "$R" | card)
[ -n "$A" ] && check "invoice created and sent" "$(approve "$A")" || bad "no send card: $(echo "$R" | errors)"

step "2. multi-recipient invoice -> plan, approve all"
R=$(chat "Invoice Meera (meera.demo@example.com) \$60 and Kabir (kabir.demo@example.com) \$35 for the workshop $TAG")
P=$(echo "$R" | plan_id)
if [ -n "$P" ]; then
  echo "$R" | py 'import sys,json
p=next(b for b in json.load(sys.stdin)["blocks"] if b["type"]=="plan")
for s in p["steps"]: print("    step", s["n"], s["status"], "-", s["summary"])'
  check "multi-recipient plan approved (2 invoices sent)" "$(post "/api/plan/$P/decide" '{"approve":true}')"
else
  bad "expected a 2-step plan: $(echo "$R" | texts)"
fi

step "3. invoice with no email (shareable pay link)"
R=$(chat "Invoice Ana \$15 for stickers $TAG"); A=$(echo "$R" | card)
[ -n "$A" ] && check "no-email invoice sent with pay link" "$(approve "$A")" || bad "no send card"

step "4. checkout link (Orders v2)"
R=$(chat "Make a checkout link for 2 T-shirts at \$15"); check "checkout link created" "$R"
URL=$(echo "$R" | link)
case "$URL" in https://www.sandbox.paypal.com/*) ok "link points at sandbox checkout";;
  */mock/checkout/*) [ "$MODE" = "mock" ] && ok "mock checkout link (dry run)" || bad "unexpected link: $URL";;
  *) bad "unexpected link: $URL";; esac
echo "    Pay it with a sandbox PERSONAL account to test capture + refund ('Refund the last order')."

step "5. who hasn't paid?"
check "unpaid summary" "$(chat "Who hasn't paid?")"

step "6. list invoices"
check "list invoices" "$(chat "List invoices")"

step "7. remind everyone unpaid"
R=$(chat "Remind everyone unpaid"); A=$(echo "$R" | card)
if [ -n "$A" ]; then
  D=$(approve "$A"); check "reminders sent" "$D"
  echo "$D" | grep -q 'Priya, Priya\|Kabir, Kabir\|Meera, Meera' && bad "reminder text repeats a name" || ok "reminder names deduplicated"
else bad "no remind card: $(echo "$R" | texts)"; fi

step "8. cancel Kabir's invoice"
R=$(chat "Cancel Kabir's invoice"); A=$(echo "$R" | card)
[ -n "$A" ] && check "invoice cancelled" "$(approve "$A")" || bad "no cancel card: $(echo "$R" | errors)"

step "9. payout (Payouts v1; blocked for India-registered merchants)"
R=$(chat "Pay \$2 to alex.demo@example.com and \$1 to sam.demo@example.com"); A=$(echo "$R" | card)
if [ -n "$A" ]; then
  D=$(approve "$A"); E=$(echo "$D" | errors); echo "$D" | texts
  if [ -z "$E" ]; then ok "payout submitted"
  elif echo "$E" | grep -q "Payouts aren't available"; then soft "payout refused for this account's country (friendly message shown)"
  else bad "payout: $E"; fi
else bad "no payout card"; fi

step "10. INR invoice to an Indian buyer (blocked for India-registered merchants)"
R=$(chat "Invoice Arjun (arjun.demo@example.com) ₹500 for website fixes $TAG"); A=$(echo "$R" | card)
E=$(echo "$R" | errors)
if [ -n "$A" ]; then D=$(approve "$A"); E=$(echo "$D" | errors); echo "$D" | texts; fi
if [ -z "$E" ]; then ok "INR invoice sent"
elif echo "$E" | grep -qi "INR\|currency"; then soft "INR blocked for this account (friendly message shown)"
else bad "INR invoice: $E"; fi

step "ledger"
curl -sS "$B/api/ledger" | py 'import sys,json;d=json.load(sys.stdin)
print("    invoices:", [(i["recipient_name"], i["amount"], i["currency"], i["display_status"]) for i in d["invoices"]][:12])
print("    orders:", [(o["id"], o["display_status"]) for o in d["orders"]][:5])
print("    owed:", d["owed"])'

echo
echo "== result: $PASS passed, $FAIL failed, $SOFT expected-by-country"
[ "$FAIL" -eq 0 ]
