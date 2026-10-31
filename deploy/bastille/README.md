# PagerMCP as a Bastille (FreeBSD) template

This installs the same PagerMCP server as the Docker image, but inside a FreeBSD
jail via [Bastille](https://bastillebsd.org), using `pkg` for dependencies and
the same environment-variable configuration.

## What the template does

- `pkg install python312 py312-aiohttp py312-markdown py312-cryptography
  py312-sqlite3` (no pip, no build step; `py312-sqlite3` is a separate package
  on FreeBSD and the app needs it).
- Creates a `pagermcp` service user and `/var/db/pagermcp/data`.
- Fetches the app source for `PAGER_VERSION` (default `0.10.0`) from the GitHub
  release tag into `/usr/local/lib/pagermcp`.
- Installs `/usr/local/etc/rc.d/pagermcp` and `/usr/local/bin/pagermcp`.
- Enables and starts the service.

## Requirements on the host

- Bastille with a bootstrapped release (`bastille bootstrap 15.1-RELEASE`).
- The jail needs **outbound network** (DNS + HTTPS) for `pkg` and the source
  fetch, and its address must be reachable by clients. A jail whose address is
  only on the loopback bridge (`bastille0`) has no route out. On a host where
  Bastille uses loopback-only networking, add the jail address as an alias on the
  external interface for outbound *and* on `bastille0` for host reachability, e.g.:

  ```sh
  ifconfig vtnet0 inet 10.13.40.201/24 alias
  ifconfig bastille0 inet 10.13.40.201/32 alias
  sysrc ifconfig_vtnet0_alias0="inet 10.13.40.201/24"
  ```

  (A VNET jail on a bridged interface is the cleaner alternative.)

## Apply

```bash
# copy this template into Bastille's template directory
install -d /usr/local/bastille/templates
tar czf - -C deploy/bastille pagermcp | ssh root@host 'tar xzf - -C /usr/local/bastille/templates'

# create a jail and apply the template
bastille create -T pager 15.1-RELEASE 10.13.40.201
bastille template -a pager pagermcp/server --arg PAGER_NAMESERVER=10.13.40.1
```

Pass `PAGER_NAMESERVER` when the host's `/etc/resolv.conf` uses `127.0.0.1`
(a loopback resolver the jail cannot use); otherwise `pkg`/`fetch` fail with DNS
errors.

Override the version or port:

```bash
bastille template -a pager pagermcp/server --arg PAGER_VERSION=0.10.0 --arg PAGER_PORT=8080
```

## Configuration (same env as Docker)

Set these in `/etc/rc.conf` inside the jail (or via `bastille sysrc`):

```
pagermcp_enable="YES"
pagermcp_port="8080"
pagermcp_data_dir="/var/db/pagermcp/data"
pagermcp_env="MESSAGE_TTL_DAYS=7 MAILBOX_TTL_DAYS=30 SUBSCRIPTION_TTL_DAYS=30 NTFY_URL=https://ntfy.example"
```

`pagermcp_env` is a space-separated list of `KEY=VALUE`; every environment
variable the server understands (`DATA_DIR`, `*_TTL_DAYS`, `MAX_*`, `SSE_*`,
`NTFY_*`, `FCM_*`, ...) can be passed this way. For FCM, point
`FCM_CREDENTIALS` at a service-account JSON file inside the jail.

## Service control

```bash
bastille service pager pagermcp status
bastille service pager pagermcp restart
```

Verify:

```bash
curl -s http://10.13.40.201:8080/version
curl -s http://10.13.40.201:8080/settings
```
