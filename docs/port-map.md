# Port map

Every file of the working version (upstream, private) and where it goes. **core** — the package; **plugin** — shipped
built-in plugin; **cc-plugin** — Claude Code plugin; **private** — stays in the author's setup; **split** — part core,
part private. Lines — upstream size at the freeze point `e77dc49` (2026-11-27, D8).

## Commands and libraries

| Upstream | Lines | Target | Notes |
| --- | ---: | --- | --- |
| `chat-link` | 1093 | core, split | link daemon → modules: routing, authority, questions, waiting list, reactions, loop guard. Answers into the task tracker → private answer-sink plugin |
| `chat-post` | 704 | core, split | post as role bot, asks with buttons, edit, pin, invite. Tracker question keys → private |
| `chat.d/edge.py` | 654 | core | buttons, dialogs, per-owner keys; its hard-coded `/unfollow` and `/bot-token` become the first `edge` commands of the registry (D6) |
| `chat.d/chatlib.py` | 332 | core, split | connections, admission, bots, relay, thread route **ported** ✅ → `src/lmost/connections.py` (19 tests; exact `cwd` from transcripts instead of decoding the directory name). Still to move: API/upload/download/describe → Mattermost transport; asks registry, snoozes, discussions → questions. `REVIEW_SESSIONS` → private admission plugin (`ADMISSION`, `BOT_RESOLVERS`); tracker env → private |
| `chat-activity` | 313 | core | typing, bot status, progress post |
| `chat-mr-watch` | 241 | plugin | GitLab MR events → inbox; `glab` dependency stays optional |
| `chat-dispatch` | 217 | core | wake dispatcher, bash → Python; terminal tab launcher hint → configurable |
| `chat-inbox` | 134 | core ✅ | **ported** → `src/lmost/inbox.py`, `lmost inbox`; 27 tests. Receipts and activity on ack — via `ACK_LISTENERS`, wired when those modules land |
| `chat.d/chatws.py` | 133 | core | Mattermost websocket watcher (transport) |
| `chat-who` | 125 | core, split | chat login lookup; GitLab side → plugin |
| `chat-agents` | 100 | core | agent directory from bot profiles |
| `chat-confirm` | 96 | core | confirmation post ✅/❌ |
| `chat-bot-add` | 93 | core | create a role bot (`mmctl`) |
| `chat-setup` | 91 | cc-plugin + core | replaced by plugin install + `lmost setup --check` |
| `chat.d/coldread.py` | 63 | private | unexplained-code check tied to a workspace glossary; maybe later as optional text-check plugin |
| `chat.d/stale_asks.py` | 63 | core | Stop reminder to cancel stale questions |
| `chat-channel` | 47 | core | channel purpose, header |
| `chat.d/turn_origin.py` | 47 | core | who started the turn, from the transcript |
| `chat-thread` | 35 | core | read a thread |
| `chat-file` | 27 | core | download an attachment |
| `chat-react` | 20 | core | receipt reactions |

## Hooks

| Upstream | Target | Notes |
| --- | --- | --- |
| `hooks/chat-inbox-hook.sh` | cc-plugin | PostToolUse / Stop delivery; Codex variant too |
| `hooks/chat-confirm-guard.sh` | cc-plugin | dangerous command in a chat-started turn needs confirmation |
| `hooks/chat-peer-guard.sh` | cc-plugin | peer-started turn is read-only |
| `hooks/chat-ask-guard.sh` | cc-plugin | blocking questions go to chat when the session has a bot |
| `hooks/chat-mr-hook.sh` | plugin | links a created MR to the session; MR-create detection made configurable |

## Config, units, tests, specs

| Upstream | Target | Notes |
| --- | --- | --- |
| `config/*.example` | core | connection, routes, confirm patterns, private patterns |
| `systemd/*` | core | + launchd examples |
| `test/*.test.sh` (57) | oracle | run against upstream during the port; rewritten as pytest per module |
| `openspec/specs/chat-authority` (8 req) | core | drop "answer to tracker question" requirement → plugin |
| `openspec/specs/chat-delivery` (18 req) | core, split | review thread routing → private; GitLab events → plugin spec |
| `openspec/specs/chat-edge` (3 req) | core | drop "server sync" (deployment-specific) |
| `openspec/specs/chat-questions` (16 req) | core | |
| `openspec/specs/chat-status` (6 req) | core | |
| `openspec/specs/chat-text` (7 req) | core, split | "MR — link with title", cold reader → plugin/private |
| `openspec/changes/*` (15 open) | — | ported one by one as they land upstream (D8) |

## Upstream specs → l-most specs

| Upstream | l-most | Dropped / moved |
| --- | --- | --- |
| chat-delivery (18) | delivery (17) | GitLab events → gitlab-mr; review routing generalised to shared bots + thread routing |
| chat-authority (8) | authority (7) | tracker answers → extensions: answer sinks |
| chat-questions (16) | questions (16) | "tracker questions" section → one section per answer sink |
| chat-status (6) | status (7) | + bridge heartbeat (NEW) |
| chat-text (7) | text (5) | cold reader → private plugin; MR links → gitlab-mr |
| chat-edge (3) | edge (4) | server sync → stale-edge warning (NEW); library lookup → packaging; + per-owner keys, keyed slash URLs |
| — | commands (6) | NEW |
| — | extensions (5) | NEW |
| — | gitlab-mr (4) | from delivery + text |
