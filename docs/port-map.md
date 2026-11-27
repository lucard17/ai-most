# Port map

Every file of the working version (upstream, private) and where it goes. **core** — the package; **plugin** — shipped
built-in plugin; **cc-plugin** — Claude Code plugin; **private** — stays in the author's setup; **split** — part core,
part private. Lines — upstream size at the freeze point `e77dc49` (2026-11-27, D8).

## Commands and libraries

| Upstream | Lines | Target | Notes |
| --- | ---: | --- | --- |
| `chat-link` | 1093 | core, split | link daemon → modules: routing, authority, questions, waiting list, reactions, loop guard. Answers into the task tracker → private answer-sink plugin; `waiting.py` ✅ — answers from edge marks, Waiting for you pin, reminders, snooze/drop, discussions, ttl, backfill; sinks via `waiting.ANSWER_SINKS`.; `link.py` ✅ (+`linkctx`, `receipts`, `cli_link`: `aimost link serve|once|routes|backfill-asks`) |
| `chat-post` | 704 | core ✅, split | **ported** → `src/aimost/{post,questions,textcheck}.py`, `aimost post`. Tracker question keys, cold reader → private; MR links → GitLab plugin (code 20); answer sinks — `Poster.ask(sink=…)` |
| `chat.d/edge.py` | 654 | core ✅ | **ported** → `src/aimost/{edge,edgecmds,commands,heartbeat}.py`, `aimost edge serve`, `aimost commands register`; buttons drawn by `questions.render` / `confirm.render_confirm` (one renderer for both sides); heartbeat in bot preferences (D13) |
| `chat.d/edge.py` `/health` (after the freeze, D8) | +749 | optional plugin ✅ | **ported** → `src/aimost_health/{config,probe,run}.py` (D15); example `data/config/health-targets.example.json`; product-specific reasons and details → the targets file |
| `chat.d/chatlib.py` | 332 | core, split | connections, admission, bots, relay, thread route **ported** ✅ → `src/aimost/connections.py` (19 tests; exact `cwd` from transcripts instead of decoding the directory name). API/upload/download/describe ported → `src/aimost/mattermost.py` ✅; asks registry, snoozes, discussions → questions. `REVIEW_SESSIONS` → private admission plugin (`ADMISSION`, `BOT_RESOLVERS`); tracker env → private |
| `chat-activity` | 313 | core ✅ | **ported** → `src/aimost/activity.py`, `aimost activity`; `begin` wired to `inbox.ACK_LISTENERS` |
| `chat-mr-watch` | 241 | plugin | GitLab MR events → inbox; `glab` dependency stays optional; `src/aimost_gitlab/watch.py` ✅ (`aimost gitlab add|rm|ls|once`) |
| `chat-dispatch` | 217 | core ✅ | **ported** → `src/aimost/dispatch.py`, `aimost dispatch once|serve`; launcher hint → `AIMOST_WAKE_HINT` |
| `chat-inbox` | 134 | core ✅ | **ported** → `src/aimost/inbox.py`, `aimost inbox`; 27 tests. Activity on ack wired via `ACK_LISTENERS`; receipts (📬/👀) — with the bridge |
| `chat.d/chatws.py` | 133 | core ✅ | **ported** → `src/aimost/ws.py` (stoppable watcher); tested on the real server |
| `chat-who` | 125 | core ✅, split | **ported** chat side → `aimost who`; other sources via `WHO_SOURCES` (GitLab → plugin) |
| `chat-agents` | 100 | core ✅ | **ported** → `src/aimost/agents.py`, `aimost agents` |
| `chat-confirm` | 96 | core ✅ | **ported** → `src/aimost/confirm.py`, `aimost confirm`; per-MR merge grants → `confirm.GRANTS` plugin; after the freeze (D8 catch-up): `--spec` fields (`confirm_spec.py`), template, `merges`-only merge coverage, MR cards → GitLab plugin (`aimost_gitlab/cards.py`) |
| `chat-bot-add` | 93 | core ✅ | **ported** → `src/aimost/botadmin.py`, `aimost bot add` — over REST with an admin token instead of ssh + `mmctl` (D12) |
| `chat-setup` | 91 | cc-plugin + core | `aimost setup [--check]` ✅ (dirs, connection check); units — with the Claude Code plugin; units, templates ✅ (`install.py`); plugin — repo root `.claude-plugin/`, `hooks/hooks.json` ✅ |
| `chat.d/coldread.py` | 63 | private | unexplained-code check tied to a workspace glossary; maybe later as optional text-check plugin |
| `chat.d/stale_asks.py` | 63 | core ✅ | **ported** → `src/aimost/stale_asks.py` |
| `chat-channel` | 47 | core ✅ | **ported** → `aimost channel` |
| `chat.d/turn_origin.py` | 47 | core ✅ | **ported** → `src/aimost/turn_origin.py` |
| `chat-thread` | 35 | core ✅ | **ported** → `aimost thread` |
| `chat-file` | 27 | core ✅ | **ported** → `aimost file` |
| `chat-react` | 20 | core ✅ | **ported** → `aimost react` |

## Hooks

| Upstream | Target | Notes |
| --- | --- | --- |
| `hooks/chat-inbox-hook.sh` | core ✅ + cc-plugin | PostToolUse / Stop delivery; Codex variant too |
| `hooks/chat-confirm-guard.sh` | core ✅ + cc-plugin | dangerous command in a chat-started turn needs confirmation |
| `hooks/chat-peer-guard.sh` | core ✅ + cc-plugin | peer-started turn is read-only |
| `hooks/chat-ask-guard.sh` | core ✅ + cc-plugin | blocking questions go to chat when the session has a bot |
| `hooks/chat-mr-hook.sh` | plugin | links a created MR to the session; MR-create detection made configurable; `src/aimost_gitlab/hook.py` ✅ (`MR_CREATE_PATTERNS`) |

Hook logic is `aimost hook inbox|ask-guard|confirm-guard|peer-guard` (`src/aimost/hooks.py`); the Claude Code plugin only
registers them.

## Config, units, tests, specs

| Upstream | Target | Notes |
| --- | --- | --- |
| `config/*.example` | core | connection, routes, confirm patterns, private patterns; `src/aimost/data/config/` ✅ (routes example — with the bridge) |
| `systemd/*` | core | + launchd examples; `src/aimost/data/{systemd,launchd}/` ✅ |
| `test/*.test.sh` (57) | oracle | run against upstream during the port; rewritten as pytest per module |
| `openspec/specs/chat-authority` (8 req) | core | drop "answer to tracker question" requirement → plugin |
| `openspec/specs/chat-delivery` (18 req) | core, split | review thread routing → private; GitLab events → plugin spec |
| `openspec/specs/chat-edge` (3 req) | core | drop "server sync" (deployment-specific) |
| `openspec/specs/chat-questions` (16 req) | core | |
| `openspec/specs/chat-status` (6 req) | core | |
| `openspec/specs/chat-text` (7 req) | core, split | "MR — link with title", cold reader → plugin/private |
| `openspec/changes/*` (15 open) | — | ported one by one as they land upstream (D8) |
| catch-up after the freeze, upstream `b5b7cb7..5fba1e5` (D8, D16) | ~1.2k | core ✅ | confirm described by fields + template + `merges`-only merge coverage (authority spec); `chat-post` markup guard → `markup.py`, text check `layout`, code 11 (text spec); `chat-shell-quote-guard` → hook `quote-guard`; orphan wake DM with `--plain`. MR cards → GitLab plugin. Dropped: the harness class of a card, the review-closing announce template, the `mr-merge` wrapper (workspace tools) |

## Upstream specs → ai-most specs

| Upstream | ai-most | Dropped / moved |
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
