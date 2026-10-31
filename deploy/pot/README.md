# PagerMCP on Pot (FreeBSD)

[Pot](https://pot.pizzamig.dev) is a FreeBSD container framework built on
**jails and ZFS**. It runs the application natively on FreeBSD (source install,
dependencies from `pkg`), with the same environment configuration as the other
deployments. Pot requires **ZFS** (`zfs.ko` and a pool).

## One-time setup

```sh
pkg install -y pot

# Pot needs a ZFS pool. With no spare disk you can use a file-backed pool:
truncate -s 6G /var/pot-vdev
zpool create -f -O compression=on -O atime=off potpool /var/pot-vdev

# Point Pot at that pool (defaults come from pot.default.conf):
cat > /usr/local/etc/pot/pot.conf <<'CONF'
POT_ZFS_ROOT=potpool/pot
POT_FS_ROOT=/opt/pot
POT_EXTIF=vtnet0
CONF

# `-p ""` skips modifying /etc/pf.conf (we add our own rule below).
pot init -p ""
pot create-base -r 15.1
```

## Install the flavour and create the pot

```sh
cp flavours/pagermcp flavours/pagermcp.sh /usr/local/etc/pot/flavours/
chmod +x /usr/local/etc/pot/flavours/pagermcp.sh

# resolver for the pot (the host's resolv.conf may point at 127.0.0.1)
printf 'nameserver 10.13.40.1\n' > /tmp/pot-resolv.conf

pot create -p pagerpot -N alias -i 10.13.40.203 -t single -b 15.1 \
    -f pagermcp -d custom:/tmp/pot-resolv.conf

pot start pagerpot
pot list
```

`-N alias` puts the address on the host's external NIC, so the pot is directly
reachable. Like the Bastille deployment, the host firewall must allow the port:

```
pass in proto tcp from any to 10.13.40.203 port 8080 flags S/SA keep state
```

```sh
pfctl -nf /etc/pf.conf && pfctl -f /etc/pf.conf
```

## Configuration

The bootstrap writes `/usr/local/etc/pagermcp.conf` inside the pot with the same
variables as the Docker image; edit it and `pot restart pagerpot` (or re-apply).
Extra settings (`NTFY_URL`, `FCM_CREDENTIALS`, TTLs, ...) go there.

`/version` reports build `55`, which identifies a Pot (source) install:
Docker `v0.10.0` = 52, `latest` = 53, Bastille = 54, **Pot = 55**.

## Verify

```sh
curl -s http://10.13.40.203:8080/version
curl -s http://10.13.40.203:8080/settings
```

## Releasing

```sh
./deploy/pot/sync.sh <version> <build>
cp flavours/pagermcp flavours/pagermcp.sh /usr/local/etc/pot/flavours/
# recreate (or recreate the flavour-applied pot) and verify
```
