#!/bin/sh
# Point the Pot flavour at a release.
#
# Usage: ./deploy/pot/sync.sh <version> <build>
set -eu

version="${1:?usage: sync.sh <version> <build>}"
build="${2:?usage: sync.sh <version> <build>}"

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
file="${here}/flavours/pagermcp.sh"

sed -i.bak \
    -e "s/^PAGER_VERSION=.*/PAGER_VERSION=\"${version}\"/" \
    -e "s/^PAGER_BUILD=.*/PAGER_BUILD=\"${build}\"/" \
    "${file}"
rm -f "${file}.bak"

echo "Pot flavour set to ${version} (build ${build}):"
grep -E '^PAGER_(VERSION|BUILD)=' "${file}"
