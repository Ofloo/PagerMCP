# Repository instructions

## Scope

- This repository is for a Docker-based pager/relay MCP for AI agents.
- Keep the existing `build.sh` build and publishing logic unchanged unless explicitly requested.
- The current build publishes `ofloo/pagermcp` for `linux/amd64` and `linux/arm64`; tags containing `-hailo` publish only `linux/arm64` with `HAILO_BUILD=true`.
- Version control is currently a local Git repository; do not assume GitHub, CI, or automatic releases.

## MCP architecture

- One Docker image supports both roles through external configuration: `MODE=server` or `MODE=client`.
- Server mode listens on configurable `PORT` and exposes the MCP/event relay and HTTP notification endpoint.
- Client mode exposes MCP to the local AI client through Docker/stdio and connects outbound to the server using `SERVER_URL` or `SERVER_HOST` plus `SERVER_PORT`.
- Client mode must not require a separately installed `npx` or other runtime; Docker is the client-side dependency.
- A Docker port mapping must not be used to infer the mode; configure `MODE` explicitly.
- Only the relay server needs an inbound exposed port; build machines send notifications outbound with standard `curl`.

## Pager protocol

- Agents should automatically use the waiting MCP tool after starting a long-running external job; users should not need to mention the MCP repeatedly.
- The waiting operation holds one MCP call open and returns when the matching notification arrives; do not implement polling.
- Use an opaque UUID as a reusable mailbox address; the server does not need a pre-registration table of valid tokens.
- Notifications are matched to the waiting operation using the opaque UUID mailbox and may include `PROJECT`, `JOB_ID`, `status`, `message`, and optional build `logs`; the AI interprets payloads.
- Build systems must be able to notify with a portable `curl -X POST` request and must not need a special client.
- Queue notifications while the client is unavailable and deliver them when it reconnects through MCP notifications.
- The client reads or creates `.pager_session` in the project directory; it must remain outside Git and must never be committed.
- Mailbox retention, message retention, queue capacity, request size, cleanup interval, listening address/port, wait timeout, and logging must be configurable through environment variables.
- Defaults agreed so far are a one-month mailbox TTL, a one-week message TTL, a maximum of 128 messages per mailbox, and a bounded payload; do not hard-code these where environment configuration is expected.
- A full mailbox rejects new messages; it must not discard existing messages.
- Keep the HTTP and MCP protocol documented in `RFC-0001-pager-protocol.md`.

## Persistence and deployment

- Support an in-memory mode when no data directory is configured, but use SQLite in configurable `DATA_DIR` when queued pages must survive restarts.
- The initial deployment target is one server replica with a persistent volume.
- Do not design SQLite as a multi-replica shared-disk database; future multi-replica support should use Redis or another shared store without changing the external pager protocol.
- Keep Docker Swarm deployment configuration compatible with externally supplied environment variables and persistent storage.
