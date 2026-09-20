# Remove cached PagerMCP images on every Swarm node.
#
# Swarm resolves an image tag to whatever digest the node has cached. A task
# that moves to another node can therefore run an older build for the same tag
# (`latest`). Purging the cached pager images on all nodes removes that
# ambiguity before the next deploy.
#
#   salt -N swarm_nodes state.apply pagermcp.purge_images
#
# Set the target group to match your topology, for example the whole swarm:
#   salt -C 'G@roles:swarm' state.apply pagermcp.purge_images

purge pager images:
  cmd.script:
    - source: salt://pagermcp/purge-images.sh
    - cwd: /root
    - shell: /bin/sh
