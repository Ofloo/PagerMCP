# Purging the per-node image cache

Swarm resolves an image tag to whatever digest each **node** has cached. A task
that moves to another node can therefore run an older build for the same tag
(`latest`). `docker pull` on one node does not cover the others. This is a
recurring operational risk, not a code bug.

## When this bites

- You push a new `latest`, pull it on the manager, redeploy, and the service
  still reports the old build in `GET /version`.
- A task reschedules to a node that never pulled the new tag.

## Recommended: pin, do not chase `latest`

Pin the version tag or digest in the stack file (`deploy/docker-stack.yml`).
A tag/digest that does not exist locally is always fetched on whichever node
the task lands, which removes the per-node ambiguity entirely.

```yaml
image: ofloo/pagermcp:v0.3.4
# or
image: ofloo/pagermcp@sha256:edb748d0...
```

## If you must use `latest`: purge all nodes

```bash
# one node (manual)
sh purge-images.sh

# all swarm nodes via Salt
salt -N <node-group> state.apply pagermcp.purge_images
# or, topology-based:
salt -C 'G@roles:swarm' state.apply pagermcp.purge_images
```

Then update the service so it pulls fresh:

```bash
docker service update --force --with-registry-auth <stack>_pagermcp
```

## Verify what is actually running

The image store is not the truth; the running task is.

```bash
docker service ps <stack>_pagermcp --no-trunc \
  --format '{{.Name}} {{.Node}} {{.CurrentState}} {{.Image}}'
curl -s https://pager.ofloo.io/version
```

`GET /version` returns both the semantic version and the build number, so you
can tie the running process to an exact push (for example `0.3.4` / `24`).

## Files

- `purge-images.sh` — removes cached `ofloo/pagermcp` images on one node.
- `purge-images.sls` — Salt state that runs the script on targeted nodes.
