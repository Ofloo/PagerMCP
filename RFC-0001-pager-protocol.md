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

`GET /mailboxes/{uuid}/messages` lists queued messages oldest-first; add `?order=newest` (or `?order=desc`) to list them newest-first without changing the queue, which stays FIFO for `/wait`, `/consume` and `/ack` (an invalid value returns `400`). `POST /mailboxes/{uuid}/consume` consumes the oldest message. `POST /mailboxes/{uuid}/ack` with `{"id": "<uuid>"}` removes one message by id (acknowledgement); without a body it removes the oldest. `GET /mailboxes/{uuid}/wait` blocks until a message is available. Every message response includes `id`, `created_at` (Unix timestamp), and `age_seconds` (whole seconds since the message was accepted), in addition to the submitted payload.

`GET /mailboxes/{uuid}/wait` accepts an optional `max_age_seconds=<n>` query parameter. When given, queued messages older than `n` seconds are skipped (they are dropped) and only a message within the window is returned; an invalid value returns `400`. Without the parameter, the oldest queued message is returned regardless of age. It also accepts `timeout_seconds=<n>`; the wait returns `408` after `n` seconds with no message. Without it, the server-wide `WAIT_TIMEOUT_SECONDS` applies (`0` means wait indefinitely). A bounded wait shorter than any reverse-proxy idle timeout is required so clients see a clean `408` instead of a `504`. The number of simultaneously open long-lived connections (`/wait` plus `/events`) is capped **per mailbox** at `MAX_CONNECTIONS_PER_MAILBOX` (default 5; `0` disables the cap). Above the cap the server answers `429` and the client should retry after another connection closes; the cap is advertised via `GET /settings` as `connections.max_per_mailbox` so clients can size their pools.

Delivery is **at-least-once**: `wait` and `notify` hand a message to a waiter but do **not** remove it from the queue. The message leaves the queue only when a client acknowledges it with `POST /ack` (or `consume`). If the client, its connection, or the reverse proxy dies between receiving a message and acknowledging it, the message stays queued and the next `wait` returns it again. Clients must therefore make delivery idempotent: they should acknowledge a message as soon as it has been handed off, and treat a repeated `id` as a retry rather than a new event. A client that never acknowledges a message will receive it again on every subsequent wait.

When `POST /notify` finds one or more blocked `wait` requests on the mailbox, each waiter is woken with the message; the message is removed when the first waiter acknowledges it, and further acknowledgements are no-ops. When no waiter is blocked, the message stays queued and is returned by the next `wait`. This replaces the earlier at-most-once handoff, in which a message was removed the moment it was handed to a waiter and was lost if that waiter had already gone away.

`GET /mailboxes/{uuid}/events` is an optional **push** transport: a Server-Sent Events stream (`Content-Type: text/event-stream`). It sends one `event: page` per message, with the same JSON as `/wait` as its `data:` line, and sends a `: keepalive` comment every `SSE_KEEPALIVE_SECONDS` (default 15) so the stream survives reverse-proxy idle timeouts (`X-Accel-Buffering: no` is set; proxies must not buffer). Push changes only *how* a client is woken, never the delivery guarantee: the message still leaves the queue only on `POST /ack`, and the full unacknowledged backlog is replayed when the stream (re)connects, so a dropped connection loses nothing. A client that cannot use SSE keeps using `/wait`; the two are interchangeable. SSE is cheaper for battery-limited clients (e.g. mobile) because a single idle stream avoids repeated long-poll reconnects.

A client that must be woken while it is **not connected** (a backgrounded or closed mobile app) can register a **push subscription** instead of holding a stream. This is separate from SSE and only meaningful where the platform provides an OS push channel.

`POST /mailboxes/{uuid}/subscriptions` registers an endpoint with a **lease**: the body is `{"channel": "<name>", "endpoint": "https://..."}` (optionally `keys` for future signed channels, e.g. Web Push) and the reply is `{"id": "...", "channel": "...", "expires": <unix>}`. To keep push on, the client must **renew before `expires`** by `POST`ing `{"id": "<id>"}` to the same route; if it never renews, the subscription is dropped and push turns itself off. This lease is deliberate: a mailbox whose owner forgets about it does not keep a permanent push hook (or its data) forever. `GET /mailboxes/{uuid}/subscriptions` lists the active subscriptions as `{id, channel, expires}` and deliberately **never returns the endpoint or token** (for `ntfy` that is the secret topic, for `fcm` the device token). The value is write-only: `POST`ing again with that `id` renews the lease, and including a new `endpoint`/`topic`/`token` **overwrites** the stored value (an empty value renews without changing it). `DELETE /mailboxes/{uuid}/subscriptions/{id}` removes one and `DELETE /mailboxes/{uuid}/subscriptions[?channel=<name>]` removes all or one channel.

On `POST /notify` the server sends each registered endpoint a **data-only trigger** — `{"type": "page", "mailbox": "<uuid>", "mailbox_hash": "<sha256>", "id": "<page id>"}` — and never the message content; the device wakes and fetches the page over SSE or `/wait`, exactly like any other client. `mailbox_hash` is `sha256("<uuid>:<unix-period>")` (default period 3600s = hourly, configurable via `MAILBOX_HASH_PERIOD_SECONDS`; the whole hash can be turned off with `MAILBOX_HASH_ENABLED=0`, in which case `/settings` reports `subscriptions.identity: "uuid"` instead of `"hash"` and `identity_period_seconds` states the period) — the rotating, non-identifying successor of the raw UUID: it changes every hour, so a push distributor cannot correlate triggers across hours, while a client that knows its mailbox UUID can recompute it (aligning its clock via `GET /settings` `server_time`). Since v0.13.0, when `MAILBOX_HASH_ENABLED=1` (the default) the trigger carries **only** `mailbox_hash` and no raw UUID at all; with the hash disabled the trigger falls back to the raw `mailbox` UUID form. Privacy note: with the hash enabled the push distributor sees nothing but a rotating, per-period identifier, never the message content nor the mailbox UUID. A trigger is best-effort: a slow or failing endpoint never blocks `notify`, and the page stays queued until it is acknowledged. An endpoint that answers `404`/`410` is removed automatically, and subscriptions expire with their mailbox. The lease length is `SUBSCRIPTION_TTL_DAYS` (default 30). Shortly before a lease ends (`SUBSCRIPTION_WARN_DAYS`, default 3) the server sends one `{"type": "warning", "reason": "subscription-expiring"}` trigger to the endpoint, so a client can renew before push turns itself off; renewing clears the warning, and a warning that cannot be delivered is retried at the next cleanup tick.

**Channels.** A subscription names one of these built-in channels; further channels (e.g. Web Push/VAPID) are additive on the same registry.

- **generic HTTP** (`unifiedpush`, `http`, `webhook`): register with `{"channel": "...", "endpoint": "https://..."}`; the endpoint receives `{"type": "page", "mailbox": "...", "mailbox_hash": "<sha256>", "id": "..."}` (e.g. a UnifiedPush distributor URL). To prevent SSRF into internal networks, generic endpoints must resolve to **public** addresses. Refused (at registration with `400`, and re-checked at every push): loopback (127/8, ::1), link-local (169.254/16, fe80::/10), private (10/8, 172.16/12, 192.168/16, fc00::/7), CGNAT (100.64.0.0/10, e.g. Tailscale), documentation (192.0.2/24, 198.51.100/24, 203.0.113/24), benchmarking (198.18/15), multicast (224/4, ff00::/8), reserved (240/4, incl. 255.255.255.255) and unspecified addresses; IPv4-mapped IPv6 is unwrapped first. Redirects are **never followed** (a 301/302 response is not retried elsewhere). Operators who deliberately push to a trusted internal/LAN distributor set `PUSH_ENDPOINT_ALLOWLIST` (comma-separated hostnames, IPs or CIDR networks; empty by default), e.g. `up.internal.example, 10.13.40.0/24`.
- **ntfy**: enabled by `NTFY_URL` (optionally `NTFY_TOKEN` for a protected topic); register with `{"channel": "ntfy", "topic": "<topic>"}`; the server publishes a content-free wake-up (`Title: PagerMCP`, body "New page in mailbox <uuid>") to `{NTFY_URL}/{topic}`.
- **fcm** (Firebase Cloud Messaging HTTP v1): enabled by a service account (`FCM_CREDENTIALS` as a JSON file path or inline, plus `FCM_PROJECT_ID` if the account carries none); register per mailbox with `{"channel": "fcm", "token": "<device token>"}`; the server sends a high-priority Android message that is **data-only by default** (the payload stays on the server) unless `FCM_INCLUDE_PAYLOAD` is set; an `UNREGISTERED`/`INVALID_ARGUMENT` reply removes the token.

`GET /settings` lists the enabled channels, the configured `ntfy_url`, and `fcm_enabled`, so a client knows what it may subscribe to.

`GET /mailboxes/{uuid}/status` is a compact, per-mailbox view: queued messages (total and unacknowledged), push on/off (subscriptions, channels, next lease expiry), the webclient mode, and the retention windows. It exposes no endpoints or secrets.

A mailbox can enable **webclient retention** with `POST /mailboxes/{uuid}/mode` `{"webclient": true}`; `GET /mailboxes/{uuid}/mode` returns `{"webclient": <bool>, "expires": <unix>}`, and `{"webclient": false}` turns it off. With it off (the default) acking behaves exactly as before. While it is on, `POST /ack` does **not** delete the message: it flags it `acked` (read) and keeps it in the queue, counting towards the message cap, and `/messages` shows it with `"acked": true`, so a web UI can keep showing read messages. New messages are queued normally; only when the queue is **full** (the 128-message cap) is the **oldest read (acked)** message evicted to make room — if there is no read message, the mailbox is genuinely full and new messages are rejected. Read copies also expire with the normal message retention. The mode carries a **lease** (`WEBCLIENT_TTL_DAYS`, default = the message retention, 7 days): the client must refresh it by posting again within that window, otherwise the mode lapses, the read copies are dropped, and the queue and ack return to normal behaviour. `/wait` only delivers **unacknowledged** messages.

## P2P store-and-forward (optional)

Clients that relay encrypted envelopes between peers may post the same envelope more than once (several peers reach the internet, or a retry). With `P2P_DEDUPE_ENABLED=true`, `POST /notify` accepts an optional idempotency envelope:

```
{"p2p": true, "p2p_id": "<uuid>", "p2p_expires_at": 1790604800, "p2p_created_at": 1790000000, "p2p_payload_sha256": "<advisory>"}
```

- `p2p_id` MUST be a stable UUID created once by the origin and reused unchanged by every relay and retry.
- `p2p_expires_at` is required (absolute Unix seconds). An already-expired message returns `410 Gone` and is stored nowhere.
- The server clamps the requested lifetime to `min(P2P_MAX_MESSAGE_AGE_SECONDS, MESSAGE_TTL_DAYS)`; a longer request returns `400`.
- Dedupe is per mailbox: the same `p2p_id` delivered to another mailbox is a different message.
- A repeat with the same `(mailbox, p2p_id)` and the same payload returns `200 {"id": "<existing id>", "duplicate": true}` and creates no second message or notification.
- The same `p2p_id` with a different payload returns `409 Conflict`.
- The dedupe cache stores ids/hashes and metadata only (never the payload), survives restarts (SQLite in `DATA_DIR`), and expires at `min(p2p_expires_at, seen_at + P2P_DEDUPE_TTL_SECONDS, seen_at + MESSAGE_TTL)`; it is capped by `P2P_DEDUPE_MAX_ENTRIES` and `P2P_DEDUPE_MAX_PER_MAILBOX` (oldest evicted first). The server computes the authoritative payload hash.
- Requests without `p2p:true` behave exactly as before and never touch the cache.
- Dedupe is best-effort: a duplicate that arrives after the cache entry expired is accepted again, so clients MUST still deduplicate incoming messages by `p2p_id`.
- `GET /settings` reports the effective `p2p` policy.

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

The P2P path requires the destination UUID to be **GPG-encrypted** to the server's public key (`GET /settings` → `p2p.public_key` + `key_id`, `gpg_enabled` defaults to true; disable with `P2P_GPG_ENABLED=0`). The keypair is generated automatically on first start in `DATA_DIR/gnupg` (valid for one year, renewed automatically 30 days before expiry). `P2P_GPG_NEWKEY=true` rotates on demand. During a rotation `/settings` exposes `legacy_public_key` + `legacy_key_id` next to the current ones; the legacy key is kept for 2x the message TTL after the new key was made, then pruned as obsolete. Both current and legacy fingerprints are recognised by WHO HAS. A P2P sender posts `mailbox_encrypted` (armored GPG message) instead of `uuid`; a plain `uuid` returns `400` while encryption is enabled, and undecryptable ciphertext also returns `400`. The server decrypts to route, and `(mailbox, p2p_id)` dedupe is unchanged.

## Federation: WHO HAS over a peer mesh

With `PAGER_PEERS` (comma-separated base URLs; empty = standalone), an instance answers **unknown** mailbox UUIDs and unknown P2P key fingerprints by querying its peers with `POST /mesh/query`:

```json
Mailbox lookup: {"time": <unixtime>, "hash": "sha256(<uuid>:<period-index>)", "query_id": "<uuid>", "hops": 3, "origin": "<base-url>", "origin_fingerprint": "<key-id>"} — the hash is computed with the timestamp carried by the query (both sides in sync). Fingerprint lookup: {"fingerprint": "<key-id>", ...}. Fingerprints are exchanged while peering (every query/response carries the instance's key id and its **public instance URL**, and the newest mapping for a fingerprint wins), so an unknown P2P `key_id` gets a direct 302 from the peer-fingerprint mapping without an extra lookup — always to the peer's **current public instance URL**, not merely the configured peer address; the WHO HAS fingerprint query is only a fallback.
```

A peer that owns the value answers `{"found": true, "instance": "<base-url>"}`; the requesting instance then answers the client with **302** to that instance (`PAGER_PUBLIC_URL` sets each instance's own public base URL). Queries carry `started` (the initial hop budget) and `hops` (remaining); a receiver rejects with `403` when `hops > started` ("hops not in sync"), and **all servers in a mesh must use the same `MESH_QUERY_HOPS`**: a query whose `started` budget differs from the receiver's own is rejected as out of sync (advertised as `mesh.max_hops` in `/settings`) or when the hop limit is exceeded (`hops > MESH_QUERY_HOPS` or an exhausted `hops <= 0`). Queries are forwarded with `hops - 1` and a `query_id` seen-set prevents loops; the originator marks its own query id as seen. When nobody owns a mailbox UUID, it is created locally as before. Only period-rotating hashes travel the mesh (`MESH_HASH_PERIOD_SECONDS`, default 86400); message payloads never leave the owning instance.

Abuse: `PAGER_PEER_BLACKLIST` (static hosts/IPs) returns `403`; a peer exceeding `MESH_PEER_MAX_QPM` queries/minute is dynamically blacklisted for `MESH_BLACKLIST_SECONDS`. `MESH_OPEN_QUERIES=0` restricts answering to configured peers (one-sided open peering is the default).

On the P2P path, `mailbox_encrypted` requires `key_id` (the fingerprint of the public key used). An unknown `key_id` triggers a `kind: "fingerprint"` WHO HAS query and a **302** to the key-owning instance; only when nobody owns it is it a `400`.

## Invites

`POST /mailboxes/{uuid}/invite` creates a human-readable slug with a TTL (`INVITE_TTL_SECONDS`, default 86400; per-invite `ttl_seconds`, max 30 days). Without `chat` the invite resolves to the plain mailbox UUID (backwards compatible). With `chat: true` it carries `pagerapp://contact?u1=<uuid>&u2=<optional>&s=<public base URL>&n=<name>&i=<sender id>` (percent-encoded UTF-8; scheme prefix via `INVITE_SCHEME` or the per-invite `scheme` field). `GET /invite/<slug>` returns the record + `uri`; `?redirect=1` issues a 302 to that URI. `GET /invite/<slug>/qr` returns an SVG QR encoding exactly that URI. Expired invites return `404` and are purged by the reaper.

## Configuration

`MODE`, `HOST`, `PORT`, `SERVER_URL`, `SERVER_HOST`, `SERVER_PORT`, `DATA_DIR`, `MAILBOX_TTL_DAYS`, `MESSAGE_TTL_DAYS`, `MAX_MESSAGES_PER_MAILBOX`, `MAX_MESSAGE_BYTES`, `WAIT_TIMEOUT_SECONDS`, `SSE_KEEPALIVE_SECONDS`, `SSE_QUEUE_MAX`, `SUBSCRIPTION_TTL_DAYS`, `SUBSCRIPTION_WARN_DAYS`, `MAX_SUBSCRIPTIONS_PER_MAILBOX`, `CLEANUP_INTERVAL_SECONDS`, `WEBCLIENT_TTL_DAYS`, `P2P_DEDUPE_ENABLED`, `P2P_DEDUPE_TTL_SECONDS`, `P2P_MAX_MESSAGE_AGE_SECONDS`, `P2P_DEDUPE_MAX_ENTRIES`, `P2P_DEDUPE_MAX_PER_MAILBOX`, `NTFY_URL`, `NTFY_TOKEN`, `FCM_CREDENTIALS`, `FCM_PROJECT_ID`, `FCM_INCLUDE_PAYLOAD`, `PUSH_ENDPOINT_ALLOWLIST`, `MAX_CONNECTIONS_PER_MAILBOX`, `PAGER_LOG_FILE`, `PAGER_LOG_RETENTION_DAYS`, `SETTINGS_HIDE_NTFY_URL`, `MAILBOX_HASH_ENABLED`, `MAILBOX_HASH_PERIOD_SECONDS`, `P2P_GPG_ENABLED`, `P2P_GPG_NEWKEY`, `PAGER_PEERS`, `PAGER_PEER_BLACKLIST`, `PAGER_PUBLIC_URL`, `MESH_HASH_PERIOD_SECONDS`, `MESH_QUERY_HOPS`, `MESH_PEER_MAX_QPM`, `MESH_BLACKLIST_SECONDS`, `MESH_OPEN_QUERIES`, `INVITE_TTL_SECONDS`, `INVITE_SCHEME`, `PAGER_UID`, and `PAGER_GID` (the uid/gid the server drops to after fixing DATA_DIR ownership; default nobody/65534) are externally configurable on the server. The client honours `PAGER_URL`, `PAGER_SESSION_FILE`, `PAGER_SESSION_ID`, `PAGER_MAX_AGE_MS`, `PAGER_WAIT_SECONDS`, and `PAGER_TRANSPORT`.
