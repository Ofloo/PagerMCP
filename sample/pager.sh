#!/bin/sh
# Generic pager wrapper: run a command, watch a PID, or notify directly,
# then send one PagerMCP notification with the result and exit with the
# command's exit code. The script never keeps running after the work is done.
#
# On failure it also tries to send a failure notification when the script
# itself is stopped (SIGTERM/SIGINT), aborts on bad input, or hits a script
# error, so a broken invocation does not disappear silently. SIGKILL of this
# script cannot be caught and produces no notification by definition; a
# command killed with SIGKILL is reported normally with exit code 137.
#
# Modes:
#   1) Watch an existing PID:
#        pager.sh --pid 1234 --message "done"
#   2) Run a command (foreground), then notify:
#        pager.sh --run "curl --upload-file f.iso https://share.example.com/f.iso" \
#                 --message "upload done" --job-id iso-upload
#   3) Pipe a command in on stdin:
#        echo "long-running-command" | pager.sh --message "done" --job-id job1
#   4) Send an immediate notification (no command):
#        echo "i'm done" | pager.sh --notify --job-id myjob
#        pager.sh --notify --message "i'm done"
#
# Options:
#   --pid <PID>            existing PID to watch
#   --run <command>        shell command to run (foreground) and report on
#   --notify               send one notification immediately (message from stdin/--message)
#   --session <uuid|file>  mailbox UUID or path to a session file (default: ./.pager_session)
#   --message <msg>        message to send (default: "process done")
#   --job-id <id>          JOB_ID (default: the pid or "notify")
#   --project <name>       PROJECT field (optional; omit when not set)
#   --success-status <s>   status on success (default: success)
#   --fail-status <s>      status on failure (default: failed)
#   --log <file>           redirect the command's output (default: /dev/null)
#   --tail <n>             include last N lines of output as "logs" (default: 10)
#   --success-cmd <cmd>    command whose stdout becomes "logs" (runs on success)
#   --interval <sec>       poll interval for --pid (default: 10)
#   --dry-run[=<code>]    simulate: print the notification instead of sending it;
#                          exit with <code> (default 0)
#   --verbose              echo the JSON payloads to stdout
#
# Environment:
#   PAGER_URL              server URL (default: https://pager.ofloo.io)
#
# Exit codes:
#   --run/--pid: the exit code of the watched command (pid: 0 when it stops)
#   --notify:    0 on success, 1 if the notification could not be sent
#   --dry-run:   the configured code (default 0)
#   stopped:     143 on SIGTERM, 130 on SIGINT (after a failure notification)

SERVER_URL="${PAGER_URL:-https://pager.ofloo.io}"
SESSION=""; UUID=""; NOTIFIED=0; EXIT_REASON=""; DRY_RUN_SET=0; HELP=0

PID=""; RUN=""; MESSAGE=""; JOB_ID=""; PROJECT=""
SUCCESS_STATUS="success"; FAIL_STATUS="failed"; SUCCESS_CMD=""
LOG=""; INTERVAL=10; TAIL=10; VERBOSE=0; NOTIFY=0; DRY_RUN=""

json_escape() {
    printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' | tr '\n' ' '
}

build_payload() {
    status="$1"; msg="$2"; logs="$3"; exit_code="$4"
    project_field=""
    [ -n "$PROJECT" ] && project_field="\"PROJECT\":$(printf '"%s"' "$(json_escape "$PROJECT")"),"
    cat <<JSON
{${project_field}"JOB_ID":"$(json_escape "$JOB_ID")","status":"$(json_escape "$status")","message":"$(json_escape "$msg")","exit_code":$exit_code,"logs":"$(json_escape "$logs")"}
JSON
}

# Low-level send. Returns curl's status; used by send_page and emergency_page.
post_page() {
    payload=$(build_payload "$@")
    if [ "$VERBOSE" -eq 1 ]; then
        echo "$payload"
    fi
    curl -fsS --max-time 10 -H "Authorization: Bearer ${UUID}" \
        -H "Content-Type: application/json" \
        -d "${payload}" "${SERVER_URL}/notify" >/dev/null 2>&1
}

# Normal send: mark that a notification was attempted so the EXIT trap does
# not send a second failure page for the same run.
send_page() {
    post_page "$@"
    rc=$?
    NOTIFIED=1
    return $rc
}

UUID_PATTERN="^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"

# Resolve UUID from --session or ./.pager_session. Returns 1 when unresolved.
resolve_uuid() {
    if [ -n "$SESSION" ]; then
        if echo "$SESSION" | grep -Eq "$UUID_PATTERN"; then
            UUID="$SESSION"
        elif [ -f "$SESSION" ]; then
            UUID=$(tr -d '\n' < "$SESSION")
        else
            return 1
        fi
    elif [ -f ".pager_session" ]; then
        UUID=$(tr -d '\n' < ".pager_session")
    else
        return 1
    fi
    [ -n "$UUID" ] || return 1
    return 0
}

# Last-resort failure page: only when no page was sent yet, not a dry run, and
# a mailbox address can be resolved. Never fails the script.
emergency_page() {
    code="$1"; reason="$2"
    [ "$NOTIFIED" -eq 1 ] && return 0
    [ "$DRY_RUN_SET" -eq 1 ] && return 0
    [ "$HELP" -eq 1 ] && return 0
    resolve_uuid || return 0
    [ -n "$JOB_ID" ] || JOB_ID="pager.sh"
    send_page "$FAIL_STATUS" "$reason" "" "$code" >/dev/null 2>&1
    return 0
}

on_exit() {
    code=$?
    [ -n "$EXIT_REASON" ] || EXIT_REASON="pager.sh aborted before notifying (exit $code)"
    emergency_page "$code" "$EXIT_REASON"
    return 0
}

on_term() {
    emergency_page 143 "pager.sh terminated by SIGTERM"
    trap - EXIT TERM INT
    exit 143
}

on_int() {
    emergency_page 130 "pager.sh interrupted by SIGINT"
    trap - EXIT TERM INT
    exit 130
}

trap 'on_exit' EXIT
trap 'on_term' TERM
trap 'on_int' INT

print_help() {
    awk 'NR > 1 { if ($0 !~ /^#/) exit; print }' "$0"
}

while [ $# -gt 0 ]; do
    case "$1" in
        --pid)            PID="$2"; shift 2 ;;
        --run)            RUN="$2"; shift 2 ;;
        --notify)         NOTIFY=1; shift ;;
        --session)        SESSION="$2"; shift 2 ;;
        --message)        MESSAGE="$2"; shift 2 ;;
        --job-id)         JOB_ID="$2"; shift 2 ;;
        --project)        PROJECT="$2"; shift 2 ;;
        --success-status) SUCCESS_STATUS="$2"; shift 2 ;;
        --fail-status)    FAIL_STATUS="$2"; shift 2 ;;
        --log)            LOG="$2"; shift 2 ;;
        --tail)           TAIL="$2"; shift 2 ;;
        --success-cmd)    SUCCESS_CMD="$2"; shift 2 ;;
        --interval)       INTERVAL="$2"; shift 2 ;;
        --dry-run)        DRY_RUN_SET=1; DRY_RUN="0"; shift ;;
        --dry-run=*)      DRY_RUN_SET=1; DRY_RUN="${1#--dry-run=}"; shift ;;
        --verbose)        VERBOSE=1; shift ;;
        -h|--help)        HELP=1; print_help; exit 0 ;;
        *) EXIT_REASON="pager.sh: unknown argument: $1"; echo "$EXIT_REASON" >&2; exit 1 ;;
    esac
done

if ! resolve_uuid; then
    if [ -n "$SESSION" ]; then
        echo "session not found: $SESSION (expected a UUID or a file)" >&2
    else
        echo "no --session given and ./.pager_session not found" >&2
    fi
    exit 1
fi

# Immediate notify mode: no command watching, just send one notification.
if [ "$NOTIFY" -eq 1 ]; then
    if [ -z "$MESSAGE" ] && [ ! -t 0 ]; then
        MESSAGE=$(cat)
    fi
    [ -z "$MESSAGE" ] && MESSAGE="process done"
    [ -z "$JOB_ID" ] && JOB_ID="notify"

    if [ "$DRY_RUN_SET" -eq 1 ]; then
        echo "$(build_payload "$SUCCESS_STATUS" "$MESSAGE" "" "$DRY_RUN")"
        exit "$DRY_RUN"
    fi
    if send_page "$SUCCESS_STATUS" "$MESSAGE" "" 0; then
        exit 0
    else
        exit 1
    fi
fi

# Pipe mode: no --pid/--run but stdin is a pipe -> read it as the command.
if [ -z "$PID" ] && [ -z "$RUN" ] && [ ! -t 0 ]; then
    RUN=$(cat)
fi

[ -z "$PID" ] && [ -z "$RUN" ] && { EXIT_REASON="pager.sh: need --pid, --run, --notify, or piped stdin"; echo "$EXIT_REASON" >&2; exit 1; }
[ -z "$MESSAGE" ] && MESSAGE="process done"

collect_logs() {
    if [ -n "$SUCCESS_CMD" ]; then
        eval "$SUCCESS_CMD" 2>&1 | tr "\n" " "
    elif [ "$TAIL" -gt 0 ] && [ -n "$LOG" ] && [ "$LOG" != "/dev/null" ] && [ -f "$LOG" ]; then
        tail -n "$TAIL" "$LOG" 2>/dev/null | tr "\n" " "
    fi
}

# Run mode: execute the command in the foreground, wait, report, exit with its code.
if [ -n "$RUN" ]; then
    [ -z "$LOG" ] && LOG=/dev/null
    [ -z "$JOB_ID" ] && JOB_ID="run"

    if [ "$DRY_RUN_SET" -eq 1 ]; then
        if [ "$DRY_RUN" -eq 0 ]; then sim_status="$SUCCESS_STATUS"; else sim_status="$FAIL_STATUS"; fi
        echo "$(build_payload "$sim_status" "$MESSAGE" "" "$DRY_RUN")"
        exit "$DRY_RUN"
    fi

    sh -c "$RUN" >"$LOG" 2>&1
    EXIT_CODE=$?
    if [ "$EXIT_CODE" -eq 0 ]; then
        STATUS="$SUCCESS_STATUS"
    else
        STATUS="$FAIL_STATUS"
    fi
    send_page "$STATUS" "$MESSAGE" "$(collect_logs)" "$EXIT_CODE"
    exit "$EXIT_CODE"
fi

# PID mode: watch an existing PID, then report once and exit.
[ -z "$JOB_ID" ] && JOB_ID="$PID"
while kill -0 "$PID" 2>/dev/null; do sleep "$INTERVAL"; done

LOGS=""
if [ -n "$SUCCESS_CMD" ]; then
    LOGS=$(collect_logs)
fi
send_page "$SUCCESS_STATUS" "$MESSAGE" "$LOGS" 0
exit 0
