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
docker run --rm -i --user "$(id -u):$(id -g)" -v "$PWD":/workspace -e MODE=client -e PROJECT_DIR=/workspace -e SERVER_URL=http://localhost:8080 ofloo/pagermcp
```

## Pager Plugin

The OpenCode Pager Plugin source is in `plugins/pager.js`. It keeps one blocking request open to PagerMCP and sends each pagerbericht to the most recently active OpenCode session.

Install it globally for all projects:
```bash
mkdir -p ~/.config/opencode/plugins
cp plugins/pager.js ~/.config/opencode/plugins/pager.js
```

The client reads or creates the current session key in `.pager_session` and repairs its ownership. Run the client with `--user "$(id -u):$(id -g)"` so the mounted project file belongs to the host user. `PAGER_UID` and `PAGER_GID` can override the ownership when the container must run as root.

Restart OpenCode after installing or updating the plugin. A successful load is written to the OpenCode log as `Pager Plugin loaded for <project-directory>`. The plugin also adds automatic system guidance explaining how the AI should interpret pagerberichten. The default PagerMCP URL is `http://10.13.17.60:6721`, so no environment variable is required. Set `PAGER_URL` only when using another server. You may also set `PAGER_SESSION_FILE` or `PAGER_SESSION_ID`. The plugin reads the UUID from each project's `.pager_session`, reconnects after errors, and logs failures through OpenCode without polling PagerMCP.
