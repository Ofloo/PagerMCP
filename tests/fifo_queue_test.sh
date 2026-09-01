#!/usr/bin/env bash
set -euo pipefail

BASE_URL="${PAGER_URL:-${SERVER_URL:-http://localhost:8080}}"
BASE_URL="${BASE_URL%/}"
SESSION_FILE="${PAGER_SESSION_FILE:-${PROJECT_DIR:-$PWD}/.pager_session}"
COUNT="${FIFO_TEST_COUNT:-3}"
PREFIX="pager-fifo-$(date +%s)-$$"

if [[ ! -r "$SESSION_FILE" ]]; then
    printf 'Session file not found: %s\n' "$SESSION_FILE" >&2
    exit 1
fi

TOKEN=$(tr -d '[:space:]' < "$SESSION_FILE")
if [[ ! "$TOKEN" =~ ^[0-9a-fA-F-]{36}$ ]]; then
    printf 'Invalid UUID in %s\n' "$SESSION_FILE" >&2
    exit 1
fi

for ((index = 1; index <= COUNT; index++)); do
    payload=$(printf '{"PROJECT":"PagerMCP","JOB_ID":"%s-%02d","status":"success","message":"FIFO test %02d"}' "$PREFIX" "$index" "$index")
    status=$(curl -sS -o /tmp/pagermcp-fifo-response.json -w '%{http_code}' -X POST "$BASE_URL/notify" -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' --data "$payload")
    if [[ "$status" != 202 ]]; then
        printf 'Notification %d failed with HTTP %s\n' "$index" "$status" >&2
        cat /tmp/pagermcp-fifo-response.json >&2
        exit 1
    fi
done

curl -fsS "$BASE_URL/mailboxes/$TOKEN/messages" -o /tmp/pagermcp-fifo-messages.json
python3 - "$PREFIX" "$COUNT" /tmp/pagermcp-fifo-messages.json <<'PY'
import json
import sys

prefix, count, path = sys.argv[1], int(sys.argv[2]), sys.argv[3]
data = json.load(open(path, encoding="utf-8"))
messages = data.get("messages", [])
positions = []
for index in range(1, count + 1):
    job_id = f"{prefix}-{index:02d}"
    matches = [position for position, message in enumerate(messages) if message.get("JOB_ID") == job_id]
    if len(matches) != 1:
        raise SystemExit(f"Expected exactly one queued message for {job_id}, found {len(matches)}")
    positions.append(matches[0])
if positions != sorted(positions):
    raise SystemExit(f"FIFO order failed: positions={positions}")
print(f"FIFO order verified for {count} messages; queued messages were left available for the client.")
PY
rm -f /tmp/pagermcp-fifo-response.json /tmp/pagermcp-fifo-messages.json
