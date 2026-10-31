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

On `notify` the server sends a **data-only trigger** (no message content) to each endpoint; the device wakes and fetches the page over SSE/`/wait`. Subscriptions use a lease (`SUBSCRIPTION_TTL_DAYS`, default 30): a client that never renews turns its own push off, so a forgotten mailbox does not keep a permanent push hook. Shortly before a lease ends (`SUBSCRIPTION_WARN_DAYS`, default 3) the server sends a `warning` trigger so the client can renew in time. The channels are a generic HTTP endpoint (e.g. a UnifiedPush distributor URL), `ntfy` (set `NTFY_URL`), and `fcm` (set `FCM_CREDENTIALS`, see below); Web Push/VAPID is additive later. The endpoint/token is **write-only**: it is never returned by the API, and you rotate it by `POST`ing a new value with the `id`.

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

`GET /settings` returns the server's effective, non-secret settings — message/mailbox limits, retention windows, the default wait timeout, SSE keepalive, push-subscription lease and warning windows, supported channels, cleanup interval, and whether storage is persistent — so a client can align itself without guessing. It exposes no addresses or secrets.

## FreeBSD / Bastille

A Bastille template that installs the same server into a FreeBSD jail — dependencies from `pkg`, the same environment configuration — lives under `deploy/bastille/` in the repository. Apply it with `bastille template <jail> pagermcp/server`; configure it with `pagermcp_port` and `pagermcp_env` in `/etc/rc.conf`. A source install reports build `0` from `/version` (there is no Docker build number).

## Privacy

PagerMCP logs nothing, but it is an open protocol without built-in security: your privacy depends entirely on keeping the mailbox UUID secret. Anyone who has a UUID can read that mailbox and send to it. The full statement is served at `GET /privacy`.
