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

`GET /mailboxes/{uuid}/messages` lists queued messages. `POST /mailboxes/{uuid}/consume` consumes the oldest message. `POST /mailboxes/{uuid}/ack` with `{"id": "<uuid>"}` removes one message by id (acknowledgement); without a body it removes the oldest. `GET /mailboxes/{uuid}/wait` blocks until a message is available. Every message response includes `id`, `created_at` (Unix timestamp), and `age_seconds` (whole seconds since the message was accepted), in addition to the submitted payload.

`GET /mailboxes/{uuid}/wait` accepts an optional `max_age_seconds=<n>` query parameter. When given, queued messages older than `n` seconds are skipped (they are dropped) and only a message within the window is returned; an invalid value returns `400`. Without the parameter, the oldest queued message is returned regardless of age. It also accepts `timeout_seconds=<n>`; the wait returns `408` after `n` seconds with no message. Without it, the server-wide `WAIT_TIMEOUT_SECONDS` applies (`0` means wait indefinitely). A bounded wait shorter than any reverse-proxy idle timeout is required so clients see a clean `408` instead of a `504`.

Delivery is **at-least-once**: `wait` and `notify` hand a message to a waiter but do **not** remove it from the queue. The message leaves the queue only when a client acknowledges it with `POST /ack` (or `consume`). If the client, its connection, or the reverse proxy dies between receiving a message and acknowledging it, the message stays queued and the next `wait` returns it again. Clients must therefore make delivery idempotent: they should acknowledge a message as soon as it has been handed off, and treat a repeated `id` as a retry rather than a new event. A client that never acknowledges a message will receive it again on every subsequent wait.

When `POST /notify` finds one or more blocked `wait` requests on the mailbox, each waiter is woken with the message; the message is removed when the first waiter acknowledges it, and further acknowledgements are no-ops. When no waiter is blocked, the message stays queued and is returned by the next `wait`. This replaces the earlier at-most-once handoff, in which a message was removed the moment it was handed to a waiter and was lost if that waiter had already gone away.

`GET /mailboxes/{uuid}/events` is an optional **push** transport: a Server-Sent Events stream (`Content-Type: text/event-stream`). It sends one `event: page` per message, with the same JSON as `/wait` as its `data:` line, and sends a `: keepalive` comment every `SSE_KEEPALIVE_SECONDS` (default 15) so the stream survives reverse-proxy idle timeouts (`X-Accel-Buffering: no` is set; proxies must not buffer). Push changes only *how* a client is woken, never the delivery guarantee: the message still leaves the queue only on `POST /ack`, and the full unacknowledged backlog is replayed when the stream (re)connects, so a dropped connection loses nothing. A client that cannot use SSE keeps using `/wait`; the two are interchangeable. SSE is cheaper for battery-limited clients (e.g. mobile) because a single idle stream avoids repeated long-poll reconnects.

A client that must be woken while it is **not connected** (a backgrounded or closed mobile app) can register a **push subscription** instead of holding a stream. This is separate from SSE and only meaningful where the platform provides an OS push channel.

`POST /mailboxes/{uuid}/subscriptions` registers an endpoint with a **lease**: the body is `{"channel": "<name>", "endpoint": "https://..."}` (optionally `keys` for future signed channels, e.g. Web Push) and the reply is `{"id": "...", "channel": "...", "expires": <unix>}`. To keep push on, the client must **renew before `expires`** by `POST`ing `{"id": "<id>"}` to the same route; if it never renews, the subscription is dropped and push turns itself off. This lease is deliberate: a mailbox whose owner forgets about it does not keep a permanent push hook (or its data) forever. `GET /mailboxes/{uuid}/subscriptions` lists the active subscriptions as `{id, channel, expires}` and deliberately **never returns the endpoint or token** (for `ntfy` that is the secret topic, for `fcm` the device token). The value is write-only: `POST`ing again with that `id` renews the lease, and including a new `endpoint`/`topic`/`token` **overwrites** the stored value (an empty value renews without changing it). `DELETE /mailboxes/{uuid}/subscriptions/{id}` removes one and `DELETE /mailboxes/{uuid}/subscriptions[?channel=<name>]` removes all or one channel.

On `POST /notify` the server sends each registered endpoint a **data-only trigger** — `{"type": "page", "mailbox": "<uuid>", "id": "<page id>"}` — and never the message content; the device wakes and fetches the page over SSE or `/wait`, exactly like any other client. A trigger is best-effort: a slow or failing endpoint never blocks `notify`, and the page stays queued until it is acknowledged. An endpoint that answers `404`/`410` is removed automatically, and subscriptions expire with their mailbox. The lease length is `SUBSCRIPTION_TTL_DAYS` (default 30). Shortly before a lease ends (`SUBSCRIPTION_WARN_DAYS`, default 3) the server sends one `{"type": "warning", "reason": "subscription-expiring"}` trigger to the endpoint, so a client can renew before push turns itself off; renewing clears the warning, and a warning that cannot be delivered is retried at the next cleanup tick.

**Channels.** A subscription names one of these built-in channels; further channels (e.g. Web Push/VAPID) are additive on the same registry.

- **generic HTTP** (`unifiedpush`, `http`, `webhook`): register with `{"channel": "...", "endpoint": "https://..."}`; the endpoint receives `{"type": "page", "mailbox": "...", "id": "..."}` (e.g. a UnifiedPush distributor URL).
- **ntfy**: enabled by `NTFY_URL` (optionally `NTFY_TOKEN` for a protected topic); register with `{"channel": "ntfy", "topic": "<topic>"}`; the server publishes a content-free wake-up (`Title: PagerMCP`, body "New page in mailbox <uuid>") to `{NTFY_URL}/{topic}`.
- **fcm** (Firebase Cloud Messaging HTTP v1): enabled by a service account (`FCM_CREDENTIALS` as a JSON file path or inline, plus `FCM_PROJECT_ID` if the account carries none); register per mailbox with `{"channel": "fcm", "token": "<device token>"}`; the server sends a high-priority Android message that is **data-only by default** (the payload stays on the server) unless `FCM_INCLUDE_PAYLOAD` is set; an `UNREGISTERED`/`INVALID_ARGUMENT` reply removes the token.

`GET /settings` lists the enabled channels, the configured `ntfy_url`, and `fcm_enabled`, so a client knows what it may subscribe to.

`GET /mailboxes/{uuid}/status` is a compact, per-mailbox view: queued messages (total and unacknowledged), push on/off (subscriptions, channels, next lease expiry), the webclient mode, and the retention windows. It exposes no endpoints or secrets.

A mailbox can enable **webclient retention** with `POST /mailboxes/{uuid}/mode` `{"webclient": true}`; `GET /mailboxes/{uuid}/mode` returns `{"webclient": <bool>, "expires": <unix>}`, and `{"webclient": false}` turns it off. With it off (the default) acking behaves exactly as before. While it is on, `POST /ack` does **not** delete the message: it flags it `acked` (read) and keeps it in the queue, counting towards the message cap, and `/messages` shows it with `"acked": true`, so a web UI can keep showing read messages. New messages are queued normally; only when the queue is **full** (the 128-message cap) is the **oldest read (acked)** message evicted to make room — if there is no read message, the mailbox is genuinely full and new messages are rejected. Read copies also expire with the normal message retention. The mode carries a **lease** (`WEBCLIENT_TTL_DAYS`, default = the message retention, 7 days): the client must refresh it by posting again within that window, otherwise the mode lapses, the read copies are dropped, and the queue and ack return to normal behaviour. `/wait` only delivers **unacknowledged** messages.

Messages left queued because no consumer was connected are delivered to the next `wait` regardless of age within the message retention window; deliver the queue oldest-first and use `age_seconds`/`created_at` to judge whether an old queued page is still relevant. Clients should surface the age to the operator (or pass `max_age_seconds`) rather than treating an old queued page as fresh.

`GET /version` returns the running semantic version, sequential build number, and the container hostname, for example `{ "version": "0.1.0", "build": "42", "hostname": "pztqiyg6f5w6" }`. The client checks this endpoint at startup and reports a mismatch without preventing connection. The hostname identifies which replica/task answered, which is useful under Swarm where several tasks can run behind one endpoint.

The build number is what identifies the exact image. A semantic version alone is ambiguous: `latest` and a release tag of the same release are separate builds with different build numbers (for example `0.3.4` build `23` for the pinned `v0.3.4` tag and `0.3.4` build `24` for `latest`). Whenever a running instance is reported — bug reports, deploy verification, upgrade checks — always record **version and build number** from this endpoint, not the tag name, and re-check it after the fix or roll-out.

`GET /settings` returns the server's **effective, non-secret settings** so a client can align itself without guessing: message-size and mailbox-depth limits, the message and mailbox retention windows, the default `wait` timeout, the SSE keepalive/queue settings, the push-subscription lease and warning windows and the supported channels, the cleanup interval, and whether storage is persistent. It exposes no addresses, tokens or credentials, and is cacheable for a minute.

`GET /` serves the README rendered as HTML. `GET /rfc` serves this protocol document as plain text. `GET /privacy` serves the privacy statement as plain text. `GET /sample/{name}` serves public sample scripts such as `pager.sh` as `application/x-sh`. All are cacheable for one hour. The sample directory is not listable; only exact file names resolve.

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

Implementations must bound request size, mailbox depth, message retention, and mailbox retention. Defaults are 64 KiB, 128 messages, 7 days, and 30 days. All are configurable through environment variables. A full mailbox rejects new messages; it does not discard existing messages. A live SSE connection's push queue is bounded too (`SSE_QUEUE_MAX`, default 256); if it overflows the stream is closed and the client reconnects and replays the backlog, so overflow costs a reconnect, never a message.

## Persistence

Without `DATA_DIR`, storage is in memory. With `DATA_DIR`, SQLite persists mailboxes and pages. The initial Swarm deployment uses one server replica and a persistent volume. Multi-replica operation requires a shared store such as Redis and does not change this protocol.

## Client lifecycle

At startup, the client reads the current session key from `.pager_session`; if absent or invalid, it requests a mailbox and creates the file with mode `0600`. The client repairs file ownership using `PAGER_UID` and `PAGER_GID`, defaulting to its current UID and GID. For Docker clients, run with `--user "$(id -u):$(id -g)"` so the mounted project file is readable by the host AI client. The file must be excluded by `.gitignore`. Queued messages are available immediately after reconnecting.

A client may receive through `GET /wait` (long-poll) or `GET /events` (SSE push); the two are interchangeable and deliver the same at-least-once messages, and a client should reconnect either after a failure. The bundled plugin tries SSE and falls back to long-poll by itself (`PAGER_TRANSPORT=auto`, or `sse`/`longpoll` to force one).

## Configuration

`MODE`, `HOST`, `PORT`, `SERVER_URL`, `SERVER_HOST`, `SERVER_PORT`, `DATA_DIR`, `MAILBOX_TTL_DAYS`, `MESSAGE_TTL_DAYS`, `MAX_MESSAGES_PER_MAILBOX`, `MAX_MESSAGE_BYTES`, `WAIT_TIMEOUT_SECONDS`, `SSE_KEEPALIVE_SECONDS`, `SSE_QUEUE_MAX`, `SUBSCRIPTION_TTL_DAYS`, `SUBSCRIPTION_WARN_DAYS`, `MAX_SUBSCRIPTIONS_PER_MAILBOX`, `CLEANUP_INTERVAL_SECONDS`, `WEBCLIENT_TTL_DAYS`, `NTFY_URL`, `NTFY_TOKEN`, `FCM_CREDENTIALS`, `FCM_PROJECT_ID`, and `FCM_INCLUDE_PAYLOAD` are externally configurable on the server. The client honours `PAGER_URL`, `PAGER_SESSION_FILE`, `PAGER_SESSION_ID`, `PAGER_MAX_AGE_MS`, `PAGER_WAIT_SECONDS`, and `PAGER_TRANSPORT`.
