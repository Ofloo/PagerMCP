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
docker run --rm -i -v "$PWD":/workspace -e MODE=client -e PROJECT_DIR=/workspace -e SERVER_URL=http://localhost:8080 ofloo/pagermcp
```
