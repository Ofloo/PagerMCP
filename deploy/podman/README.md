# PagerMCP on Podman (Linux)

This runs the **same OCI image** as the Docker deployment, pulled from Docker Hub
(`docker.io/ofloo/pagermcp:v0.10.0`). Podman is native on Linux. On FreeBSD the
`podman` port runs through `ocijail` (experimental), so use the Bastille/Pot
deployment there instead.

## Quadlet (systemd)

Quadlet turns the files in `/etc/containers/systemd/` into systemd services.

```sh
sudo cp pagermcp.container pagermcp.volume /etc/containers/systemd/
sudo systemctl daemon-reload
sudo systemctl start pagermcp
sudo systemctl enable pagermcp        # start on boot ([Install] WantedBy)
```

Rootless: copy the same files to `~/.config/containers/systemd/` and use
`systemctl --user daemon-reload && systemctl --user start pagermcp` (port 8080 is
unprivileged, so this works without root).

## Plain Podman (no systemd)

```sh
./run.sh 0.10.0
```

## Configuration

The same environment variables as the Docker image. Edit the `Environment=`
lines in `pagermcp.container`, or add a drop-in
`/etc/containers/systemd/pagermcp.container.d/extra.conf`:

```
[Container]
Environment=NTFY_URL=https://ntfy.example
Environment=FCM_CREDENTIALS=/run/secrets/fcm.json
Secret=fcm_json,type=mount,target=fcm.json
```

Create the FCM secret from the service-account JSON:

```sh
podman secret create fcm_json /path/to/service-account.json
```

`pagermcp.env.example` lists every variable; for `podman run` you can pass a copy
with `--env-file pagermcp.env`.

## Verify

```sh
curl -s http://127.0.0.1:8080/version
curl -s http://127.0.0.1:8080/settings
```

The version/build are the image's own (`v0.10.0` = build 52), identical to the
Docker tag — Podman pulls the same manifest.

## Releasing

Each release just points at the new tag:

```sh
./deploy/podman/sync.sh <version>
git add deploy/podman && git commit -m "Podman deployment: <version>" && git push
```
