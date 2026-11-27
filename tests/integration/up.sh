#!/usr/bin/env bash
# Start the disposable Mattermost and create the test world: team `it`, people `owner` (admin), `colleague`, `outsider`,
# bots `deployer`, `devops`, `review`, `relay`, public channel `agents` with everyone in it; tokens go to tests/integration/it.env (gitignored), read by the integration tests.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
port=${LMOST_IT_PORT:-8067}
dc() { docker compose -f "$here/compose.yaml" "$@"; }
dc up -d --wait --wait-timeout 180
mm() { dc exec -T app mmctl --local "$@"; }
for u in owner colleague outsider; do
  mm user create --email "$u@example.test" --username "$u" --password "Passw0rd-it-$u" $([ "$u" = owner ] && echo --system-admin) >/dev/null
done
mm team create --name it --display-name IT >/dev/null
mm team users add it owner colleague outsider
tok() { mm token generate "$1" it --json | sed -nE 's/.*"token": *"([^"]+)".*/\1/p'; }
{
  echo "LMOST_IT_URL=http://127.0.0.1:$port"
  echo "LMOST_IT_TEAM=it"
  for u in owner colleague outsider; do echo "LMOST_IT_TOKEN_$(echo $u | tr a-z A-Z)=$(tok $u)"; done
  # bots: mmctl cannot create them in local mode — REST with the admin's token
  admin=$(tok owner); url="http://127.0.0.1:$port/api/v4"
  rest() { curl -sf -H "Authorization: Bearer $admin" -H 'Content-Type: application/json' "$@"; }
  team_id=$(rest "$url/teams/name/it" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
  for b in deployer devops review relay; do
    bid=$(rest -X POST "$url/bots" -d "{\"username\":\"$b\",\"display_name\":\"$b\",\"description\":\"test bot $b\"}" \
          | python3 -c 'import json,sys; print(json.load(sys.stdin)["user_id"])')
    rest -X POST "$url/teams/$team_id/members" -d "{\"team_id\":\"$team_id\",\"user_id\":\"$bid\"}" >/dev/null
    btok=$(rest -X POST "$url/users/$bid/tokens" -d '{"description":"it"}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')
    echo "LMOST_IT_BOT_$(echo $b | tr a-z A-Z)=$btok"
    echo "LMOST_IT_BOT_ID_$(echo $b | tr a-z A-Z)=$bid"
    bots="${bots:-} $bid"
  done
  ch=$(rest -X POST "$url/channels" -d "{\"team_id\":\"$team_id\",\"name\":\"agents\",\"display_name\":\"agents\",\"type\":\"O\"}" \
       | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
  for u in owner colleague outsider; do
    uid=$(rest "$url/users/username/$u" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')
    echo "LMOST_IT_USER_ID_$(echo $u | tr a-z A-Z)=$uid"
    rest -X POST "$url/channels/$ch/members" -d "{\"user_id\":\"$uid\"}" >/dev/null
  done
  for bid in $bots; do rest -X POST "$url/channels/$ch/members" -d "{\"user_id\":\"$bid\"}" >/dev/null; done
  echo "LMOST_IT_CHANNEL_AGENTS=$ch"
  echo "LMOST_IT_TEAM_ID=$team_id"
} > "$here/it.env"
chmod 600 "$here/it.env"
grep -c '=' "$here/it.env" | xargs echo "it.env lines:"
