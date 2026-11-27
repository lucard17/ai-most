#!/usr/bin/env bash
# Add the edge to a running test world (called by up.sh; safe to run again): bot `edge` (system admin — it patches other
# bots' posts and posts into DMs), random keys, edge.env for the container and AIMOST_IT_EDGE_* lines in it.env; then
# (re)start only the edge container. Mattermost and its data are not touched.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
dc() { docker compose -f "$here/compose.yaml" --profile edge "$@"; }
env_get() { sed -n "s/^$1=//p" "$here/it.env"; }
url="$(env_get AIMOST_IT_URL)/api/v4"; admin=$(env_get AIMOST_IT_TOKEN_OWNER)
rest() { curl -sf -H "Authorization: Bearer $admin" -H 'Content-Type: application/json' "$@"; }
py() { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }
bid=$(rest "$url/users/username/edge" 2>/dev/null | py 'd["id"]' 2>/dev/null || true)
if [ -z "$bid" ]; then
  bid=$(rest -X POST "$url/bots" -d '{"username":"edge","display_name":"edge","description":"ai-most edge (test)"}' | py 'd["user_id"]')
fi
docker compose -f "$here/compose.yaml" exec -T app mmctl --local roles system_admin edge >/dev/null
btok=$(rest -X POST "$url/users/$bid/tokens" -d '{"description":"it-edge"}' | py 'd["token"]')
rnd() { python3 -c 'import secrets; print(secrets.token_hex(16))'; }
master=$(rnd); secret=$(rnd); slash=$(rnd)
umask 077
cat > "$here/edge.env" <<EOT
EDGE_BOT_TOKEN=$btok
EDGE_MASTER=$master
EDGE_SECRET=$secret
EDGE_SLASH_SECRET=$slash
EDGE_OWNER=owner
EOT
# it.env is read by every test run: rewrite it in place (it may be a symlink), edge lines replaced, nothing else changed
python3 - "$here/it.env" "$bid" "$master" "$secret" "$slash" "${AIMOST_IT_EDGE_PORT:-8068}" <<'PY'
import sys
path, bid, master, secret, slash, port = sys.argv[1:]
keep = [ln for ln in open(path).read().splitlines() if not ln.startswith("AIMOST_IT_EDGE_")]
keep += [f"AIMOST_IT_EDGE_URL=http://127.0.0.1:{port}", "AIMOST_IT_EDGE_PUBLIC=edge:8767", f"AIMOST_IT_EDGE_BOT_ID={bid}",
         f"AIMOST_IT_EDGE_MASTER={master}", f"AIMOST_IT_EDGE_SECRET={secret}", f"AIMOST_IT_EDGE_SLASH_SECRET={slash}"]
with open(path, "r+") as f:
    f.seek(0); f.write("\n".join(keep) + "\n"); f.truncate()
PY
dc up -d --no-deps --force-recreate edge >/dev/null 2>&1
for _ in $(seq 50); do curl -sf "http://127.0.0.1:${AIMOST_IT_EDGE_PORT:-8068}/version" >/dev/null && break; sleep 0.2; done
curl -sf "http://127.0.0.1:${AIMOST_IT_EDGE_PORT:-8068}/version" | xargs -0 echo "edge up:"
