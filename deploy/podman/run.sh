#!/bin/sh
# Run PagerMCP with Podman without systemd/Quadlet. Podman pulls the same OCI
# image from Docker Hub as the Docker deployment.
#
# Usage: ./run.sh [version]
set -eu

version="${1:-0.10.0}"

podman run -d --name pagermcp \
    -p 8080:8080 \
    -v pagermcp_data:/data:Z \
    -e MODE=server \
    -e PORT=8080 \
    -e DATA_DIR=/data \
    -e MAILBOX_TTL_DAYS=30 \
    -e MESSAGE_TTL_DAYS=7 \
    -e MAX_MESSAGES_PER_MAILBOX=128 \
    -e MAX_MESSAGE_BYTES=65536 \
    -e WAIT_TIMEOUT_SECONDS=90 \
    -e SUBSCRIPTION_TTL_DAYS=30 \
    -e SUBSCRIPTION_WARN_DAYS=3 \
    -e CLEANUP_INTERVAL_SECONDS=3600 \
    "docker.io/ofloo/pagermcp:v${version}"

podman ps --filter name=pagermcp
echo "Verify: curl -s http://127.0.0.1:8080/version"
