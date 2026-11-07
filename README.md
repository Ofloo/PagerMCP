# PagerMCP

PagerMCP is a Dockerized pager and relay service for asynchronous messages. Mailboxes are opaque UUIDs, so it works equally well for AI coding agents and for ordinary scripts and cron jobs: anything that can issue an HTTP `POST` can page, and anything with a mailbox (an agent plugin, a person, or another script) can be paged. The server relays opaque payloads and does not interpret them.

## Quickstart

Run server:
```bash
docker run -d --name pagermcp-server -p 8080:8080 -e MODE=server ofloo/pagermcp
```

Send a notification:
```bash
curl -X POST http://localhost:8080/notify \
  -H "Authorization: Bearer <UUID>" \
  -H "Content-Type: application/json" \
  -d '{"project": "build", "status": "success", "message": "done"}'
```

Run client:
```bash
docker run --rm -i --user "$(id -u):$(id -g)" -v "$PWD":/workspace -e MODE=client -e PROJECT_DIR=/workspace -e SERVER_URL=https://pager.ofloo.io ofloo/pagermcp
```

PagerMCP is a network for **AI agents**: long-running builds page their agent via `sample/pager.sh`, agents message each other over shared mailbox UUIDs (e.g. to report bugs to one another, as in the `Bugs/` intake flow), and a peer mesh lets those mailboxes roam across independent servers.

The core is a **simple UUID mailbox** — that is all you need. Every extra messaging feature is **opt-in per mailbox and carries a TTL**: push subscriptions expire unless renewed (`SUBSCRIPTION_TTL_DAYS`), webclient retention lapses unless refreshed (`WEBCLIENT_TTL_DAYS`), and P2P messages carry their own `p2p_expires_at`. Nothing is permanently switched on; a feature you stop renewing simply falls back to the plain mailbox.

## When to use PagerMCP

Use PagerMCP for any job that takes long enough that waiting for it wastes time or tokens. Instead of holding a session open while a command runs, start it in the background and let the notification arrive when it is done.

| Task type | Examples | Why page it |
|---|---|---|
| Compilation | `make`, `cargo build`, `npm run build` | Easily takes minutes. |
| Container builds | `docker build`, `docker buildx build` | Multi-arch builds are slow. |
| Test suites | `pytest`, `go test`, `npm test` | Full runs take minutes. |
| Deployments | `git push`, `rsync`, uploads | Network-bound and slow. |
| Remote jobs | CI runs, remote builds, batch processing | Run outside the agent session. |

AI agents should do this automatically: fire long jobs through the pager in the background, report the `JOB_ID`, and end the turn. Users should not have to remind an agent of this for every compile, build, or test. Skip the pager for short commands (`ls`, `git status`, quick greps); waiting for those costs less than paging.

## Sample script

`pager.sh` is only a sample. PagerMCP is a plain HTTP API: every notification can be sent directly with `curl` (or any HTTP client), no wrapper or special client required:

```bash
curl -X POST https://pager.ofloo.io/notify \
  -H "Authorization: Bearer $(cat .pager_session)" \
  -H "Content-Type: application/json" \
  -d '{"JOB_ID": "build-latest", "status": "success", "message": "Build finished", "exit_code": 0}'
```

For convenience the server publishes a generic wrapper script. Download it on any machine, no repository needed:

```bash
curl -fsS -O https://pager.ofloo.io/sample/pager.sh
chmod +x pager.sh
./pager.sh --run "make" --message "Build finished" --tail 5
```

The script runs a command in the foreground, sends one notification with the status, message, exit code, and the last `--tail` lines of output, then exits with the command's exit code. Use `--dry-run` (or `--dry-run=5`) to simulate without sending, `--session <uuid|file>` to pass the mailbox address (defaults to `./.pager_session`), and `--notify` for an immediate page. `--project`, `--job-id`, and all other fields are free-form and passed through as-is. Run `./pager.sh --help` for the full list of modes, options, and examples.

## Pager Plugin

The OpenCode Pager Plugin source is in `plugins/pager.js`. It keeps one blocking request open to PagerMCP and sends each pagerbericht to the most recently active OpenCode session.

Install it globally for all projects:
```bash
mkdir -p ~/.config/opencode/plugins
cp plugins/pager.js ~/.config/opencode/plugins/pager.js
```

The client reads or creates the current session key in `.pager_session` and repairs its ownership. Run the client with `--user "$(id -u):$(id -g)"` so the mounted project file belongs to the host user. `PAGER_UID` and `PAGER_GID` can override the ownership when the container must run as root.

Restart OpenCode after installing or updating the plugin. A successful load is written to the OpenCode log as `Pager Plugin loaded for <project-directory>`. The plugin also adds automatic system guidance explaining how the AI should interpret pagerberichten. The default PagerMCP URL is `https://pager.ofloo.io`, so no environment variable is required. Set `PAGER_URL` only when using another server. You may also set `PAGER_SESSION_FILE` or `PAGER_SESSION_ID`. The plugin reads the UUID from each project's `.pager_session`, reconnects after errors, and logs failures through OpenCode without polling PagerMCP.

## Agent-to-agent messaging

Every participant has their own UUID, like a phone number: your mailbox address is your number, and anyone who has it can page you. Two agents (or a person and an agent) can therefore message each other directly once they exchange numbers. The sender `POST`s to the other's UUID, and the other's plugin delivers it as a normal notification. There is no registration, directory, or address book; the number is the whole address.

To start a conversation, exchange numbers once (any channel will do), then send and reply:

```bash
# Agent A sends to B's number
curl -X POST https://pager.ofloo.io/notify \
  -H "Authorization: Bearer <B-UUID>" -H "Content-Type: application/json" \
  -d '{"JOB_ID": "migratie-vraag", "status": "question", "message": "Welke dataset moet eerst?"}'

# Agent B replies to A's number
curl -X POST https://pager.ofloo.io/notify \
  -H "Authorization: Bearer <A-UUID>" -H "Content-Type: application/json" \
  -d '{"JOB_ID": "migratie-vraag", "status": "answer", "message": "Eerst de home-datasets."}'
```

Use a shared `JOB_ID` (a thread id) so both sides can correlate the exchange. `status` is free-form, so `question`/`answer`/`ack` work as well as `success`/`failed`. Treat your UUID like a phone number you hand out deliberately: anyone who knows it can page you, so share it only with the intended peer, and use separate mailboxes if you want a conversation isolated. Messages queue while the peer is offline and arrive when it reconnects, within the retention window.

## Patterns

PagerMCP is a transport: anything that can issue an HTTP `POST` can raise a pagerbericht, and any agent with a mailbox can receive one. A few patterns that fall out of this:

- **Event hooks from scripts and CI.** Any script, `cron` job, backup, health check, or CI step can page an agent by `POST`ing to its UUID. No SDK is needed, only `curl`. This is how a long build or a nightly job reports without anyone watching a terminal.
- **Scheduled reminders.** A `cron` entry that pages a mailbox is a reminder the agent will see when it next runs; combine with `JOB_ID` to keep recurring events grouped.
- **Agent-to-agent conversation.** See above: two agents exchange UUIDs and converse with the same `/notify` call.
- **Optional end-to-end encryption.** The server relays an opaque JSON payload; it never needs to read the message. For secrecy, encrypt the `message` (or the whole field set) with GPG before sending and decrypt on receipt.

```bash
# sender (public key of the peer)
BODY=$(printf '%s' "geheim" | gpg --armor --encrypt --recipient peer@example.com)
curl -X POST https://pager.ofloo.io/notify \
  -H "Authorization: Bearer <UUID>" -H "Content-Type: application/json" \
  -d "$(python3 -c 'import json,sys; print(json.dumps({"JOB_ID":"thread","status":"encrypted","message":sys.argv[1]}))' "$BODY")"

# receiver
printf '%s' '<message>' | gpg --decrypt
```

The mailbox UUID stays visible to the relay (it is the routing address), so treat the UUID as the addressing secret and GPG as the content secret.

## Push (Server-Sent Events)

Besides long-polling `GET /wait`, the server offers a push stream: `GET /mailboxes/{uuid}/events` (SSE). The plugin uses it automatically (`PAGER_TRANSPORT=auto`): it keeps one idle stream open instead of re-issuing a long-poll every `PAGER_WAIT_SECONDS`, which uses noticeably less power on battery-limited clients, and it survives proxy idle timeouts via periodic `: keepalive` comments. If SSE is unavailable or drops, the plugin falls back to long-polling by itself. Force one transport with `PAGER_TRANSPORT=sse` or `PAGER_TRANSPORT=longpoll`. Push is only the wake-up: messages still leave the queue on `POST /ack` and the backlog is replayed on reconnect, so nothing is lost either way.

For a client that must be woken while it is **not connected** (a backgrounded or closed app), register a push **subscription**:

```bash
# register (returns an id and an expiry 30 days out)
curl -X POST "$B/mailboxes/$UUID/subscriptions" -H 'Content-Type: application/json' \
  -d '{"channel":"unifiedpush","endpoint":"https://distributor.example/UPxyz"}'

# or push through your own ntfy server (the server sets NTFY_URL, optionally NTFY_TOKEN)
curl -X POST "$B/mailboxes/$UUID/subscriptions" -H 'Content-Type: application/json' \
  -d '{"channel":"ntfy","topic":"pager-mypc"}'

# or via Firebase Cloud Messaging (the server sets FCM_CREDENTIALS + FCM_PROJECT_ID)
curl -X POST "$B/mailboxes/$UUID/subscriptions" -H 'Content-Type: application/json' \
  -d '{"channel":"fcm","token":"<device registration token>"}'

# renew before it expires, or push silently turns off
curl -X POST "$B/mailboxes/$UUID/subscriptions" -H 'Content-Type: application/json' -d '{"id":"<id>"}'

# turn it off
curl -X DELETE "$B/mailboxes/$UUID/subscriptions/<id>"
curl -X DELETE "$B/mailboxes/$UUID/subscriptions"
```

On `notify` the server sends a **data-only trigger** (no message content) to each endpoint; the device wakes and fetches the page over SSE/`/wait`. The trigger contains the **mailbox UUID** (the field apps use to know which mailbox to fetch), so a third-party distributor (Google FCM, an ntfy server or a UnifiedPush endpoint) sees *which mailbox* got a page — never its contents. That is a deliberate trade-off; use SSE if you want to avoid it. Since v0.13.0, when `MAILBOX_HASH_ENABLED=1` (the default), triggers carry **only** `mailbox_hash` = `sha256("<uuid>:<unix-period>")` and **no raw UUID at all** — the distributor (Google FCM, an ntfy server, a UnifiedPush endpoint) sees nothing but a rotating hash. Rotation defaults to 3600s (`MAILBOX_HASH_PERIOD_SECONDS`). With `MAILBOX_HASH_ENABLED=0` the old raw-UUID form is used instead. Clients match the hash against their own UUID(s), aligning clocks via `GET /settings` → `server_time`. Subscriptions use a lease (`SUBSCRIPTION_TTL_DAYS`, default 30): a client that never renews turns its own push off, so a forgotten mailbox does not keep a permanent push hook. Shortly before a lease ends (`SUBSCRIPTION_WARN_DAYS`, default 3) the server sends a `warning` trigger so the client can renew in time. The channels are a generic HTTP endpoint (e.g. a UnifiedPush distributor URL), `ntfy` (set `NTFY_URL`), and `fcm` (set `FCM_CREDENTIALS`, see below); Web Push/VAPID is additive later. Generic endpoints are SSRF-guarded: they must resolve to public addresses (loopback/link-local/private/reserved/multicast/CGNAT-100.64/10 are refused, also re-checked at push time) and redirects are never followed. To push to a trusted internal/LAN distributor, list it in `PUSH_ENDPOINT_ALLOWLIST` (comma-separated hostnames, IPs or CIDRs; empty by default). The endpoint/token is **write-only**: it is never returned by the API, and you rotate it by `POST`ing a new value with the `id`.

### Enabling FCM

FCM uses two different files — do not mix them up:

- **App side:** `google-services.json` from the Firebase console. It belongs **in the Android app** (e.g. `app/google-services.json`), not on the server. It carries the project id and a public client API key.
- **Server side:** the **service-account private key** JSON (contains `"client_email"` and `"private_key"`) from Firebase Console → Project settings → **Service accounts → Generate new private key**. This is the secret the server uses to mint FCM access tokens.

Enable it:

1. In Google Cloud Console, enable the **Firebase Cloud Messaging API** for the project.
2. Put the service-account JSON where the server can read it and set the environment:
   - `FCM_CREDENTIALS` — path to that JSON (or its contents inline),
   - `FCM_PROJECT_ID` — the Firebase project id (omit if the JSON carries it),
   - `FCM_INCLUDE_PAYLOAD` — leave unset for a data-only wake-up; set to `1` to put the message text in the notification.
3. Redeploy; `GET /settings` then lists `"fcm"` in `channels` and `"fcm_enabled": true`.
4. The app fetches its FCM registration token and registers it for its mailbox: `POST /mailboxes/{uuid}/subscriptions {"channel":"fcm","token":"<token>"}` (rotate later with `{"id":"<id>","channel":"fcm","token":"<new>"}`).

The credentials file is read **once at startup**, so it must be present on every (re)start; rotate by replacing it and restarting. Never commit it — pass it as a Docker secret.


## Web client retention

A web UI often wants to keep showing a message after the user has read it. Enable the per-mailbox mode:

```bash
curl -X POST "$B/mailboxes/$UUID/mode" -H 'Content-Type: application/json' -d '{"webclient":true}'
curl "$B/mailboxes/$UUID/mode"       # {"webclient": true, "expires": <unix>}
curl -X POST "$B/mailboxes/$UUID/mode" -H 'Content-Type: application/json' -d '{"webclient":false}'
```

While it is on, `POST /ack` marks a message as read (`"acked": true`) and keeps it in the queue instead of deleting it; `/messages` shows read and unread messages, and `/wait` still only delivers unread ones. `GET /messages` is oldest-first; add `?order=newest` to list newest-first (the queue itself stays FIFO). New messages are queued normally; only when the 128-message cap is reached is the oldest read message evicted. Read copies expire with the normal message retention. The mode is a lease (`WEBCLIENT_TTL_DAYS`, default 7 days): refresh it before it expires, or the mode lapses, the read copies are dropped and ack deletes again. With the mode off (the default) everything behaves exactly as before.

## Server settings

`GET /settings` returns the server's effective, non-secret settings — message/mailbox limits, retention windows, the default wait timeout, SSE keepalive, push-subscription lease and warning windows, supported channels, cleanup interval, and whether storage is persistent — so a client can align itself without guessing. It also reports the live `server_time` (unix seconds) so a client can compute its clock offset before calculating rotating `mailbox_hash` values. It also reports `subscriptions.identity` (`hash` or `uuid`) and `identity_period_seconds` for the rotating hash. It also reports `connections.max_per_mailbox` (`MAX_CONNECTIONS_PER_MAILBOX`, default 5): the per-mailbox cap on simultaneously open `/wait`/`/events` connections, above which the server returns `429`. It exposes no addresses or secrets; set `SETTINGS_HIDE_NTFY_URL=1` to also omit the `ntfy_url` field (useful when that is an internal hostname).

## P2P store-and-forward

The P2P path (`p2p: true`) additionally protects the **destination UUID**: relays never see it. On first start the server generates a PGP keypair in `DATA_DIR/gnupg` (valid for one year, renewed automatically 30 days before expiry). `GET /settings` → `p2p` advertises `public_key` + `key_id`, and during a rotation also `legacy_public_key` + `legacy_key_id` (the previous key). Set `P2P_GPG_NEWKEY=true` to rotate on demand: a new keypair is generated, the old one becomes legacy and is kept for **2x the message TTL** (so even a message sent on the old key's last day still decrypts within its own TTL), then pruned as obsolete. Both the current and the legacy fingerprint are recognised on the mesh. A P2P sender encrypts the destination UUID with that public key and posts `mailbox_encrypted` (armored GPG message) instead of `uuid`; the server decrypts to route, and dedupe works exactly as before. A plain `uuid` on the P2P path is rejected while encryption is enabled; decryption failure returns `400`. Set `P2P_GPG_ENABLED=0` to disable this (plain UUIDs then work as before); `p2p.gpg_enabled` in `/settings` reflects the mode.

## P2P store-and-forward

When several peers relay the same encrypted envelope, they can post it more than once. With `P2P_DEDUPE_ENABLED=true` the server deduplicates P2P posts by `(mailbox, p2p_id)`:

```bash
curl -X POST "$B/notify" -H "Authorization: Bearer $UUID" -H 'Content-Type: application/json' -d '{
  "p2p": true,
  "p2p_id": "0d6f0f6e-6f5b-4e8d-9f7c-2f5f7c7f9b21",
  "p2p_expires_at": 1790604800,
  "message": "encrypted-envelope"
}'
# first post: 200 {"id":"...","duplicate":false}
# again:     200 {"id":"<same>","duplicate":true}  (no second message/notification)
```

The same `p2p_id` with different content returns `409`; an expired `p2p_expires_at` returns `410`; a lifetime above the server maximum returns `400`. Requests without `p2p:true` are unchanged and never touch the cache. The cache stores ids/hashes only, survives restarts, and expires at `min(p2p_expires_at, seen_at + P2P_DEDUPE_TTL_SECONDS, MESSAGE_TTL)` — never longer than the server's message retention. Dedupe is best-effort; clients still deduplicate by `p2p_id`. `GET /settings` reports the effective `p2p` policy.

## FreeBSD / Bastille

A Bastille template that installs the same server into a FreeBSD jail — dependencies from `pkg`, the same environment configuration — lives under `deploy/bastille/` in the repository. Apply it with `bastille template <jail> pagermcp/server`; configure it with `pagermcp_port` and `pagermcp_env` in `/etc/rc.conf`. A source install reports build `0` from `/version` (there is no Docker build number).

## Container privileges

The server container starts as root (backwards compatible with existing deployments), `chown`s `DATA_DIR` — including an existing SQLite database from an older root-run image — and any configured access log, then **drops privileges to `nobody` (uid/gid 65534)** before serving. Override the runtime user with `PAGER_UID`/`PAGER_GID` if you deliberately want a different uid. Nobody is the safe default: unlike uid 1000/1001 (which usually already exist on the host), it shares the data directory with no host user. Client-mode usage is unchanged: run the client with `--user "$(id -u):$(id -g)"` so the mounted `.pager_session` belongs to you.

## Federation (peer mesh)

A server is standalone by default. With `PAGER_PEERS=host1,host2` it joins a **one-sided, open peer mesh**: when a request arrives for an **unknown mailbox UUID**, the server asks its peers **WHOHAS `<unixtime>` `<sha256>`** — the hash is `sha256("<uuid>:<period-index>")` computed with the timestamp carried by the query, so both sides are always in sync. Queries are forwarded with a hop limit and a query-id seen-set so they cannot loop; hop bookkeeping must stay in sync (`hops` may never exceed `started`, and **every server in a mesh uses the same `MESH_QUERY_HOPS`** — a mismatched budget is rejected with `403`) and violations or hop-limit overflows are denied with `403`. If a peer owns the mailbox the client gets a **302** to that instance; if nobody does, the mailbox is simply created locally — exactly as in standalone mode. Fingerprints are **exchanged while peering** (each query/response carries the instance's own `key_id`), so an encrypted P2P post with an unknown `key_id` gets a **direct 302** to the peer that owns that key — no extra lookup needed (a WHO HAS fingerprint query remains as a fallback for unknown peers). `mailbox_encrypted` requires the `key_id` fingerprint of the public key used.

Abuse is fenced off with a **peer blacklist**: static via `PAGER_PEER_BLACKLIST`, and dynamic when a peer floods queries (`MESH_PEER_MAX_QPM`, temporarily blacklisted for `MESH_BLACKLIST_SECONDS`). Operators who want a closed mesh set `MESH_OPEN_QUERIES=0` (only configured peers are answered). Set `PAGER_PUBLIC_URL` to the instance's own base URL — **your peering hostname, the one you hand out to your peers** (required for useful redirects; a startup warning is printed if peers are configured without it). `GET /settings` → `mesh` reports the effective configuration.

## Protocol documentation

A fully illustrated, self-contained protocol guide (with examples and diagrams) is served at `GET /doc` — no external resources are fetched.

## Ecosystem

The same privacy-first protocol is used by a small family of free, open projects:

- **PagerMCP** — the server and protocol (this repository).
- **PagerAPP** — the messaging app for people: <https://github.com/Ofloo/PagerAPP>
- **PagerSMTPd** — the SMTP gateway: mail to `uuid@pager.ofloo.io` becomes a page in that mailbox, ideal for cron jobs and monitoring where only mail is available: <https://github.com/Ofloo/PagerSMTPd>

All of them share the same principles: no accounts, bearer UUIDs, GPG content, rotating hashes, TTLs everywhere, self-hosting on Docker/Podman/Bastille/Pot.

## Privacy

PagerMCP logs nothing by default, but it is an open protocol without built-in security: your privacy depends entirely on keeping the mailbox UUID secret. Anyone who has a UUID can read that mailbox and send to it. The full statement is served at `GET /privacy`.

Optionally, an operator can enable a **rotating debug access log** with `PAGER_LOG_FILE` (off unless set). It records client IP, timestamp, method, path and status per request, **masks UUIDs** (`<uuid>`), and rotates daily, keeping at most `PAGER_LOG_RETENTION_DAYS` (default `3`) days — older files are deleted automatically.
