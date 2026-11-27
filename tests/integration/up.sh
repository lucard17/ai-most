#!/usr/bin/env bash
# Start the disposable Mattermost and create the test world: team `it`, people `owner` (admin) and `colleague`,
# bots `deployer` and `relay`; tokens go to tests/integration/it.env (gitignored), read by the integration tests.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
port=${LMOST_IT_PORT:-8067}
dc() { docker compose -f "$here/compose.yaml" "$@"; }
dc up -d --wait --wait-timeout 180
mm() { dc exec -T app mmctl --local "$@"; }
for u in owner colleague; do
  mm user create --email "$u@example.test" --username "$u" --password "Passw0rd-it-$u" $([ "$u" = owner ] && echo --system-admin) >/dev/null
done
mm team create --name it --display-name IT >/dev/null
mm team users add it owner colleague
tok() { mm token generate "$1" it --json | sed -nE 's/.*"token": *"([^"]+)".*/\1/p'; }
{
  echo "LMOST_IT_URL=http://127.0.0.1:$port"
  echo "LMOST_IT_TEAM=it"
  for u in owner colleague; do echo "LMOST_IT_TOKEN_$(echo $u | tr a-z A-Z)=$(tok $u)"; done
  # bots: mmctl cannot create them in local mode — REST with the admin's token
  admin=$(tok owner); url="http://127.0.0.1:$port/api/v4"
  rest() { curl -sf -H "Authorization: Bearer $admin" -H 'Content-Type: application/json' "$@"; }
  team_id=$(rest "$url/teams/name/it" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
  for b in deployer relay; do
    bid=$(rest -X POST "$url/bots" -d "{\"username\":\"$b\",\"display_name\":\"$b\",\"description\":\"test bot $b\"}" \
          | python3 -c 'import json,sys; print(json.load(sys.stdin)["user_id"])')
    rest -X POST "$url/teams/$team_id/members" -d "{\"team_id\":\"$team_id\",\"user_id\":\"$bid\"}" >/dev/null
    btok=$(rest -X POST "$url/users/$bid/tokens" -d '{"description":"it"}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')
    echo "LMOST_IT_BOT_$(echo $b | tr a-z A-Z)=$btok"
  done
} > "$here/it.env"
chmod 600 "$here/it.env"
grep -c '=' "$here/it.env" | xargs echo "it.env lines:"
