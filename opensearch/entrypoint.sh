#!/bin/bash
# Host settings, then the official entrypoint.
# vm.max_map_count must be 262144 on the host (persist it in /etc/sysctl.d/99-opensearch.conf).
# Do not set node.store.allow_mmap: false; that was only for the 65530 default.
set -euo pipefail
CFG=/usr/share/opensearch/config/opensearch.yml
if ! grep -q '^bootstrap.memory_lock:' "$CFG"; then
  printf 'bootstrap.memory_lock: false\n' >> "$CFG"
fi
exec /usr/share/opensearch/opensearch-docker-entrypoint.sh "$@"
