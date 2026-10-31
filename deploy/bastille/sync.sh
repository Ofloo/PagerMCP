#!/bin/sh
# Point the Bastille template at a release.
#
# Usage: ./deploy/bastille/sync.sh <version> <build>
#
# <build> is the build number the jail should report from /version. Use the next
# free sequential number (the one after the release's tag and latest builds) and
# reserve it by writing it to .build_number, so the next Docker build does not
# reuse it.
set -eu

version="${1:?usage: sync.sh <version> <build>}"
build="${2:?usage: sync.sh <version> <build>}"

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
file="${here}/pagermcp/server/Bastillefile"

sed -i.bak \
    -e "s/^ARG PAGER_VERSION=.*/ARG PAGER_VERSION=${version}/" \
    -e "s/^ARG PAGER_BUILD=.*/ARG PAGER_BUILD=${build}/" \
    "${file}"
rm -f "${file}.bak"

echo "Bastille template set to ${version} (build ${build}):"
grep -E '^ARG PAGER_(VERSION|BUILD)=' "${file}"
echo
echo "Apply it on the host:"
echo "  tar czf - -C deploy/bastille pagermcp | ssh root@<host> 'tar xzf - -C /usr/local/bastille/templates'"
echo "  ssh root@<host> 'bastille template -a <jail> pagermcp/server --arg PAGER_NAMESERVER=<dns>'"
