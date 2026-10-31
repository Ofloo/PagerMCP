#!/bin/sh
# Point the Podman deployment at a release. Podman pulls the same Docker Hub
# image, so there is no build step here: just the version tag.
#
# Usage: ./deploy/podman/sync.sh <version>
set -eu

version="${1:?usage: sync.sh <version>}"

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

sed -i.bak -e "s|^Image=.*|Image=docker.io/ofloo/pagermcp:v${version}|" "${here}/pagermcp.container"
rm -f "${here}/pagermcp.container.bak"

sed -i.bak -e "s|version=\"\${1:-[0-9.]*}\"|version=\"\${1:-${version}}\"|" "${here}/run.sh"
rm -f "${here}/run.sh.bak"

echo "Podman deployment set to v${version}:"
grep -E '^Image=' "${here}/pagermcp.container"
grep -E '^version=' "${here}/run.sh"
