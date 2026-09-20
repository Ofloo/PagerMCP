# RFC-0001: Pager Protocol

## Status
Draft.

## Abstract
PagerMCP transports bounded asynchronous messages from external jobs to an AI agent through a UUID-addressed mailbox.

## Design
A server exposes HTTP endpoints. A client connects outbound and stores its reusable mailbox UUID in `.pager_session` in the project directory. The UUID is an opaque mailbox address and bearer credential; it is never committed to Git.

The server does not interpret messages. Payloads may contain `PROJECT`, `JOB_ID`, `status`, `message`, and `logs`; the AI decides what they mean.

## HTTP API

`POST /mailboxes` creates a UUID and returns `{ "uuid": "..." }`.

`POST /notify` accepts JSON and requires `Authorization: Bearer <UUID>`. Accepted messages return `202`; malformed or oversized messages return `400` or `413`; a full mailbox returns `429`.

`GET /mailboxes/{uuid}/messages` lists queued messages. `POST /mailboxes/{uuid}/consume` consumes the oldest message. `GET /mailboxes/{uuid}/wait` blocks until a message is available. Every message response includes `id`, `created_at` (Unix timestamp), and `age_seconds` (whole seconds since the message was accepted), in addition to the submitted payload.

`GET /mailboxes/{uuid}/wait` accepts an optional `max_age_seconds=<n>` query parameter. When given, queued messages older than `n` seconds are skipped (they still expire through the normal message retention) and only a message within the window is returned; an invalid value returns `400`. Without the parameter, the oldest queued message is returned regardless of age.

Delivery is at-most-once per message `id`: a message handed to a blocked `wait` request is removed from the queue in the same operation, so a subsequent `wait` cannot return it again. When `POST /notify` finds one or more blocked `wait` requests on the mailbox, each waiter receives the message once and the message is removed; when no waiter is blocked, the message stays queued and is returned by the next `wait` or `consume`. Clients must treat a repeated `id` as a protocol violation, not as a new event.

Messages left queued because no consumer was connected are delivered to the next `wait` regardless of age within the message retention window; deliver the queue oldest-first and use `age_seconds`/`created_at` to judge whether an old queued page is still relevant. Clients should surface the age to the operator (or pass `max_age_seconds`) rather than treating an old queued page as fresh.

`GET /version` returns the running semantic version and sequential build number, for example `{ "version": "0.1.0", "build": "42" }`. The client checks this endpoint at startup and reports a mismatch without preventing connection.

`GET /` serves the README rendered as HTML. `GET /rfc` serves this protocol document as plain text. `GET /sample/{name}` serves public sample scripts such as `pager.sh` as `application/x-sh`. All are cacheable for one hour. The sample directory is not listable; only exact file names resolve.

## Sample wrapper

`sample/pager.sh` is a generic, self-contained notification wrapper published by the server at `/sample/pager.sh`. It requires only `curl` and a POSIX shell.

- Modes: run a command in the foreground (`--run`), watch an existing PID (`--pid`), or send an immediate page (`--notify`).
- The mailbox address comes from `--session <uuid|file>`, defaulting to `./.pager_session`.
- The server URL defaults to `https://pager.ofloo.io` and can be overridden with `PAGER_URL` or `--session`-adjacent environment configuration.
- Every notification includes `status`, `message`, and `exit_code`; the last `N` lines of output are attached as `logs` when `--log` and `--tail` are used (default tail: 10).
- `PROJECT` and `JOB_ID` are free-form fields; `PROJECT` is omitted from the payload unless `--project` is given.
- The wrapper exits with the exit code of the command it ran and never keeps running after the command finishes.
- `--dry-run` (or `--dry-run=<code>`) prints the notification instead of sending it and exits with the configured code (default `0`).

The wrapper also reports failures of the wrapper itself, so a broken invocation does not disappear silently:

- A command killed by a signal is reported normally with the signal-derived exit code (for example `137` for SIGKILL on the command).
- On SIGTERM or SIGINT the wrapper sends one failure page with status `failed` and exit code `143` or `130` before exiting.
- Bad input (unknown option, no mode, missing or unreadable session) sends one failure page with status `failed` and a message describing the problem.
- Any other unexpected exit before a page was sent triggers one last failure page through an exit trap.
- The trap does not double-send: once a page has been sent, no second failure page is emitted.
- Limits: SIGKILL of the wrapper itself cannot be caught and produces no page. A syntax error in the published script itself that occurs before the trap is installed also cannot be caught; CI checks the script with `sh -n`.
- `--help` prints the script's own header comment, so the option list stays in sync with the code.

## Limits and retention

Implementations must bound request size, mailbox depth, message retention, and mailbox retention. Defaults are 64 KiB, 128 messages, 7 days, and 30 days. All are configurable through environment variables. A full mailbox rejects new messages; it does not discard existing messages.

## Persistence

Without `DATA_DIR`, storage is in memory. With `DATA_DIR`, SQLite persists mailboxes and pages. The initial Swarm deployment uses one server replica and a persistent volume. Multi-replica operation requires a shared store such as Redis and does not change this protocol.

## Client lifecycle

At startup, the client reads the current session key from `.pager_session`; if absent or invalid, it requests a mailbox and creates the file with mode `0600`. The client repairs file ownership using `PAGER_UID` and `PAGER_GID`, defaulting to its current UID and GID. For Docker clients, run with `--user "$(id -u):$(id -g)"` so the mounted project file is readable by the host AI client. The file must be excluded by `.gitignore`. Queued messages are available immediately after reconnecting.

## Configuration

`MODE`, `HOST`, `PORT`, `SERVER_URL`, `SERVER_HOST`, `SERVER_PORT`, `DATA_DIR`, `MAILBOX_TTL_DAYS`, `MESSAGE_TTL_DAYS`, `MAX_MESSAGES_PER_MAILBOX`, `MAX_MESSAGE_BYTES`, and `WAIT_TIMEOUT_SECONDS` are externally configurable.
