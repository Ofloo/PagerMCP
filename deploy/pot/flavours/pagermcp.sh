#!/bin/sh
# PagerMCP bootstrap, executed inside the pot at creation (as root, chrooted).
# Same application and environment configuration as the Docker/Bastille deployments.
set -eu

PAGER_VERSION="0.10.0"
PAGER_BUILD="55"
PAGER_PORT="8080"

if [ -w /etc/pkg/FreeBSD.conf ]; then
    sed -i '' 's#/quarterly#/latest#' /etc/pkg/FreeBSD.conf
fi
ASSUME_ALWAYS_YES=yes pkg bootstrap
pkg install -y python312 py312-aiohttp py312-markdown py312-cryptography py312-sqlite3
pkg clean -y

pw groupadd -n pagermcp 2>/dev/null || true
pw useradd -n pagermcp -g pagermcp -d /var/db/pagermcp -s /usr/sbin/nologin -c "PagerMCP" 2>/dev/null || true
mkdir -p /var/db/pagermcp/data /usr/local/lib/pagermcp /usr/local/etc
chown -R pagermcp:pagermcp /var/db/pagermcp

fetch -q -o /tmp/pagermcp.tgz "https://github.com/Ofloo/PagerMCP/archive/refs/tags/v${PAGER_VERSION}.tar.gz"
tar -xzf /tmp/pagermcp.tgz -C /tmp
cp -R "/tmp/PagerMCP-${PAGER_VERSION}/pager_mcp" /usr/local/lib/pagermcp/
cp "/tmp/PagerMCP-${PAGER_VERSION}/README.md" "/tmp/PagerMCP-${PAGER_VERSION}/RFC-0001-pager-protocol.md" /usr/local/lib/pagermcp/
cp -R "/tmp/PagerMCP-${PAGER_VERSION}/sample" /usr/local/lib/pagermcp/
rm -rf /tmp/pagermcp.tgz "/tmp/PagerMCP-${PAGER_VERSION}"

cat > /usr/local/etc/pagermcp.conf <<CONF
MODE=server
PORT=${PAGER_PORT}
DATA_DIR=/var/db/pagermcp/data
MAILBOX_TTL_DAYS=30
MESSAGE_TTL_DAYS=7
MAX_MESSAGES_PER_MAILBOX=128
MAX_MESSAGE_BYTES=65536
WAIT_TIMEOUT_SECONDS=90
SUBSCRIPTION_TTL_DAYS=30
SUBSCRIPTION_WARN_DAYS=3
CLEANUP_INTERVAL_SECONDS=3600
PAGERMCP_BUILD=${PAGER_BUILD}
CONF

cat > /usr/local/bin/pagermcp <<'WRAP'
#!/bin/sh
if [ -f /usr/local/etc/pagermcp.conf ]; then
    set -a
    . /usr/local/etc/pagermcp.conf
    set +a
fi
: "${MODE:=server}"; : "${PORT:=8080}"; : "${DATA_DIR:=/var/db/pagermcp/data}"
export MODE PORT DATA_DIR HOST=0.0.0.0 PYTHONPATH=/usr/local/lib/pagermcp
export HOSTNAME="$(hostname)"
cd /usr/local/lib/pagermcp || exit 1
exec su -m pagermcp -c "/usr/local/bin/python3.12 -m pager_mcp.server"
WRAP
chmod 755 /usr/local/bin/pagermcp
