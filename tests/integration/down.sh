#!/usr/bin/env bash
# Stop the disposable Mattermost; tmpfs data is gone with it.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
docker compose -f "$here/compose.yaml" down -v --remove-orphans
rm -f "$here/it.env"
