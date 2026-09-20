# PagerMCP

PagerMCP is a Dockerized pager and relay MCP server designed for AI coding agents.

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
