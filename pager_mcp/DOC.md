# PagerMCP — the protocol, illustrated

PagerMCP is a tiny, privacy-first message pager. A **mailbox** is an opaque
**UUID** — that UUID is both your *address* and your *credential*. There are no
accounts, no registration, no user database. Messages wait in a per-mailbox
FIFO queue and are delivered **at-least-once**: only an explicit `POST /ack`
removes them.

Everything on this page is served by your own server. No external resources
are fetched — the stylesheet is inline.

## 1. Quickstart

```bash
B=https://pager.ofloo.io

# create a mailbox (this UUID is your address and your secret)
UUID=$(curl -s -X POST "$B/mailboxes" | jq -r .uuid)

# somebody pages you (a build system, an agent, a friend)
curl -X POST "$B/notify" -H "Authorization: Bearer $UUID" \
  -H 'Content-Type: application/json' \
  -d '{"message":"build finished","status":"success"}'

# wait for the next page (long-poll), then acknowledge it
curl "$B/mailboxes/$UUID/wait?timeout_seconds=30"
curl -X POST "$B/mailboxes/$UUID/ack"
```

That is the entire core: `POST /mailboxes`, `POST /notify`,
`GET /mailboxes/{uuid}/wait`, `POST /ack`.

## 2. Delivery: at-least-once

```
sender                PagerMCP                       client
  |  POST /notify       |                              |
  |-------------------->|  enqueue (FIFO, persistent)  |
  |                     |                              |
  |                     |<-- GET /wait or /events -----|
  |                     |--- page (stays queued) ----->|
  |                     |                              |
  |                     |<---------------- POST /ack --|
  |                     |  page is removed             |
```

A page is **not** removed on delivery. If your client dies between `/wait` and
`/ack`, the page is still there and will be delivered again. Duplicates are
expected and should be deduplicated by `id` on the client.

## 3. Transports

| Transport | Endpoint | Use it when |
|---|---|---|
| Long-poll | `GET /mailboxes/{uuid}/wait` | simplest; works everywhere |
| SSE | `GET /mailboxes/{uuid}/events` | one idle stream; battery-friendly |
| Push subscription | `POST /mailboxes/{uuid}/subscriptions` | wake a sleeping device (FCM/ntfy/generic) |

- `/wait` returns `408` on timeout; poll again. `timeout_seconds=` is bounded
  by the server (`WAIT_TIMEOUT_SECONDS`).
- `/events` replays the unacknowledged backlog on (re)connect and sends
  keepalives (`SSE_KEEPALIVE_SECONDS`).
- Long-lived connections are capped **per mailbox**
  (`MAX_CONNECTIONS_PER_MAILBOX`, default 5; overflow = `429`).

## 4. Push subscriptions and the rotating `mailbox_hash`

A push subscription only **wakes** your device; the content is fetched over
SSE or `/wait` afterwards. Triggers are **data-only**.

```
PagerMCP                     distributor                device
  |  page arrives              |                        |
  |  {"type":"page",           |                        |
  |   "mailbox_hash":"<sha>"}  |                        |
  |--------------------------->|  (sees only a hash)    |
  |                            |----------------------->|  wake
  |                            |                        |-- GET /wait -->|
```

Since v0.13.0 the trigger carries **only** the rotating hash, never the raw
UUID:

```
mailbox_hash = sha256("<mailbox-uuid>:<unix-period>")
```

- The period defaults to **3600s** (hourly), set by `MAILBOX_HASH_PERIOD_SECONDS`.
- Your client computes the same hash for its own UUID(s) and matches.
- Clock alignment comes from `GET /settings` → `server_time`; the period is in
  `subscriptions.identity_period_seconds`, and `subscriptions.identity` says
  `"hash"` or `"uuid"`.
- Disable it with `MAILBOX_HASH_ENABLED=0` (then triggers carry the raw UUID).

Subscriptions have a **lease** (`SUBSCRIPTION_TTL_DAYS`, default 30 days): renew
or push turns itself off. A `warning` trigger arrives `SUBSCRIPTION_WARN_DAYS`
before expiry.

## 5. P2P: store-and-forward with encrypted destination

P2P messages are deduplicated per `(mailbox, p2p_id)` (`P2P_DEDUPE_ENABLED=1`).
Relays forward the same envelope, so **the destination UUID is encrypted to
the owning server's public key**:

```
sender                relays                    owning instance
  | 1. GET /settings → p2p.public_key + key_id    |
  | 2. gpg --encrypt -r <key_id>  (destination UUID)
  |                                                |
  |-- {p2p:true, key_id, mailbox_encrypted} ------>|  (ciphertext only)
  |                                                | 3. decrypt → route
  |                                                | 4. dedupe by p2p_id
```

- `public_key` and `key_id` come from `GET /settings` → `p2p`.
- `mailbox_encrypted` is the armored GPG message; `key_id` is required when
  encrypting (it tells the server which private key to use).
- Keys are generated on first start in `DATA_DIR/gnupg`, valid for one year,
  renewed automatically 30 days before expiry (old keys still decrypt).
- A plain `uuid` on the P2P path is rejected while encryption is on; set
  `P2P_GPG_ENABLED=0` to allow it.

## 6. `/settings` — configure yourself

A client can configure itself entirely from `GET /settings`:

| Field | Meaning |
|---|---|
| `version`, `build` | server version and build number |
| `server_time` | live unix time, for clock-offset alignment |
| `limits`, `retention` | message/mailbox sizes and TTLs |
| `wait`, `sse` | long-poll timeout, SSE keepalive |
| `connections.max_per_mailbox` | cap on open `/wait`+`/events` |
| `subscriptions` | channels, lease/warn, `identity`, `identity_period_seconds` |
| `p2p` | dedupe policy, `gpg_enabled`, `public_key`, `key_id` |
| `persistence` | whether a `DATA_DIR` SQLite store is used |

## 7. Retention and limits

- Messages expire after `MESSAGE_TTL_DAYS` (default 7), idle mailboxes after
  `MAILBOX_TTL_DAYS` (default 30).
- A mailbox holds at most `MAX_MESSAGES_PER_MAILBOX` (default 128) messages;
  a full mailbox rejects new pages, it never drops old ones.
- Payloads are bounded by `MAX_MESSAGE_BYTES` (default 65536).

## 8. Privacy model

- The **UUID is the only credential**: whoever has it can read and write your
  mailbox. Share it like a password. Encrypt message content with GPG for real
  end-to-end secrecy.
- Push distributors see **only a rotating hash**, never content or UUID.
- P2P relays see **only ciphertext**, never the destination UUID.
- **No logs by default.** An operator can enable a rotating debug access log
  (`PAGER_LOG_FILE`): UUIDs are masked and old files expire after
  `PAGER_LOG_RETENTION_DAYS` (default 3) days.
- This documentation page is fully self-contained: no third-party requests.

## 9. Federation (design)

A server can be pointed at a hub (`HUB_HOST`; empty = standalone): on an
unknown UUID it asks the hub — using a hash, never the raw UUID — and either
creates the mailbox locally or answers **302** to the owning instance. The
hub is only a directory (hash → instance fingerprint); messages themselves
never travel through it.

## 10. Handy endpoints

| Endpoint | Purpose |
|---|---|
| `GET /healthz` | liveness |
| `GET /version` | version + build + hostname |
| `GET /settings` | self-configuration |
| `GET /` | the README, rendered |
| `GET /rfc` | the protocol RFC (plain text) |
| `GET /privacy` | the privacy statement |
| `GET /doc` | this page |
| `GET /sample/pager.sh` | the wrapper for build jobs |
