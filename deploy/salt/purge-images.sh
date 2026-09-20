#!/bin/sh
# purge-images.sh — remove locally cached ofloo/pagermcp images on one node.
#
# Swarm caches images per node, so a tag such as `latest` can resolve to an
# older digest on a node that has not pulled recently. Run this on every node
# (for example via Salt) before a deploy, then let the service pull a fresh
# digest. See deploy/salt/README.md.
set -eu

log() { printf '%s\n' "[pagermcp-purge] $*"; }

images=$(docker images --format '{{.Repository}}:{{.Tag}}' | grep '^ofloo/pagermcp:' || true)
if [ -z "$images" ]; then
    log "no ofloo/pagermcp images cached on $(hostname)"
    exit 0
fi

log "cached on $(hostname): $(printf '%s' "$images" | tr '\n' ' ')"

# Remove every pagermcp image reference; Swarm pulls again on deploy.
for ref in $images; do
    log "removing $ref"
    docker rmi -f "$ref" >/dev/null 2>&1 || log "could not remove $ref (in use?)"
done

log "done on $(hostname)"
