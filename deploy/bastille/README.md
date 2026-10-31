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
- An address that is routed on the **external** interface (not only on the
  loopback bridge `bastille0`, which has no uplink). A VNET jail on a bridge is
  the cleanest; for a non-VNET jail put the address on the external interface and
  bind the jail to it:

  ```sh
  ifconfig vtnet0 inet 10.13.40.201/24 alias
  sysrc ifconfig_vtnet0_alias0="inet 10.13.40.201/24"
  bastille config pager set ip4.addr 'vtnet0|10.13.40.201'
  ```

  The jail needs outbound network for `pkg` and the source fetch; DNS must be
  reachable (see `PAGER_NAMESERVER`).
- The host firewall must allow the service port. Bastille's `/etc/pf.conf`
  blocks all inbound except SSH, so add a rule and reload:

  ```
  pass in proto tcp from any to 10.13.40.201 port 8080 flags S/SA keep state
  ```

  ```sh
  pfctl -nf /etc/pf.conf && pfctl -f /etc/pf.conf
  ```

  (`bastille rdr` is an alternative for forwarding a host port.)

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
bastille template -a pager pagermcp/server \
  --arg PAGER_VERSION=0.10.0 --arg PAGER_BUILD=54 --arg PAGER_PORT=8080
```

`PAGER_BUILD` is the build number `/version` reports. Docker tags use their
sequential build number (`v0.10.0` = 52, `latest` = 53); a Bastille install uses
the next number (54), so the install type is distinguishable from the build.

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

## Releasing

Every release also updates this template so a jail runs the same version:

```sh
./deploy/bastille/sync.sh <version> <build>   # sets PAGER_VERSION / PAGER_BUILD
printf '%s\n' <build> > .build_number         # reserve the number for Bastille
git add deploy/bastille && git commit -m "Bastille template: <version> build <build>" && git push
```

`<build>` is the next free sequential number (the one after the release's tag and
`latest` builds), so the jail's build stays distinguishable from Docker's. Then
copy and apply the template on the host and verify `/version` reports
`<version>` build `<build>`.

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
