# Decisions

Decisions taken before any code was moved. Each entry: what, why, what it rules out.

## D1. Open source under MIT, author owns the code — 2026-11-27

The bridge was started on 2026-11-20 in the author's personal repository, on the author's own Mattermost server, as a
personal initiative; it moved into a company tool repository on 2026-11-23. There is no written contract covering this work,
so the exclusive rights stay with the author. The project is published as the original; the company keeps a fork.

- License: **MIT** — the norm among similar bridges; the company uses its fork without friction.
- Public history starts fresh. No internal hostnames, channel names, people, or company-specific logic in code, docs or history.
- Nothing from other paid work of the author goes in (only code written for this bridge).

## D2. Name: `ai-most` — 2026-11-27

"most" is Russian for "bridge"; "ai-most" reads as "almost" in English and says what it is for. Package `ai-most`, command
and module `aimost`, props `aimost_*`, environment `AIMOST_*`. First named `l-most` (the author's initial); renamed before
publication, while no one depended on the names (PyPI `ai-most`/`aimost` and the GitHub repository were free).
Rejected: `ai-bridge` (generic, says nothing about Mattermost), `matter-most` (confusingly similar to the Mattermost trademark), names starting with `claude-` (look official).

## D3. Language: Python, port with cleanup, not a rewrite — 2026-11-27

- Runtime: Python ≥ 3.8 standard library only — installs without dependencies. Dev-only deps allowed (pytest).
- Bash logic (`chat-inbox`, `chat-dispatch`, `chat-setup`, hook bodies) moves to Python; hooks stay as 3–5 line shims.
- The 1100-line link daemon is split into modules (routing, authority, questions, waiting list, reactions).
- Linux and macOS: locking via `fcntl`, not the `flock` binary; services as a foreground process + example units for
  systemd and launchd.
- Install: `uv tool install ai-most` / `pipx install ai-most`.

Rejected: TypeScript (native MCP SDK, but a rewrite and Python is the common language around agents), Go (single binary,
most expensive port).

## D4. Delivery: hooks now, Channels behind an interface later — 2026-11-27

Events reach a Claude Code session through hooks (PostToolUse for urgent, Stop for the rest) and the wake dispatcher, as in
the working version. Delivery and wake-up are a replaceable interface. A Channels (MCP) adapter comes when Channels leaves
research preview **and** works for API-token accounts (on 2026-11-20 it did not).

## D5. Scope of v1 — 2026-11-27

In:
- **Core** — session inbox, routing rules, sender roles (owner / peer / person), questions with buttons, confirmation ✅/❌
  before dangerous actions in a chat-started turn, peer turn = read-only, edge service next to Mattermost, one bot per role,
  several chat servers (connections), activity status, receipts by reactions.
- **GitLab MR source** — shipped as a built-in plugin (example of an event source).
- **Mattermost slash commands** — new: a command registry in the core (see D6).
- **Claude Code plugin** — `/inbox`, hooks and skills installed as one plugin instead of a setup script editing settings.

Out (stay in the author's private setup as external plugins): task-tracker answers (`plan`), review-session routing,
terminal tab launcher, workspace-specific text checks.

## D6. Extension model — 2026-11-27

One pattern — *registry + interface*, plugins found via Python entry points and a local `plugins/` directory:

| Extension point | Interface | First implementations |
| --- | --- | --- |
| Chat transport | read posts, post, buttons, reactions, users | Mattermost |
| Event source | write a typed event to a session inbox | GitLab MR |
| Delivery / wake | deliver events to a running session, wake an idle one | Claude Code hooks; Codex hooks |
| Slash command | name, args, who may call, handler kind | built-in commands |
| Answer sink | where a button answer goes besides the asker's inbox | — (author's tracker plugin, private) |

Slash command kinds follow where a command can run (Mattermost calls only the edge; the owner's machine is reachable only
through its outbound connection): `edge` (runs next to the server — `/unfollow`, `/bot-token` already work this way
upstream; `/agent status` and `/agent who` too, answered from the bridge heartbeat in bot props, so they work while
the owner's machine is off), `bridge` (the edge leaves a signed command post in the caller's DM with the bot; the owner's bridge answers it),
`session` (same post, becomes a `command` event in the role's inbox). A command post older than 10 minutes is not run
silently: the bridge asks the caller with buttons whether it is still needed (decided 2026-11-27). **Authority is checked by the core, never by the
command** — a command cannot bypass confirmation or the peer read-only rule. Spec: `openspec/specs/commands`.

## D7. Order of work — 2026-11-27

1. Port map: every upstream file → core / plugin / stays private ([port-map.md](port-map.md)).
2. Specs in English — core only, with the extension interfaces added.
3. Port module by module: spec scenarios → pytest → code. The 57 upstream shell tests run against the old code as the
   behavioural oracle: same input, same observable result.
4. Packaging: Claude Code plugin, `uv`/`pipx` install, CI on Linux and macOS.
5. Publish on GitHub; the company tool repository switches to the package and keeps its private plugins.

OpenSpec stays the canon for behaviour changes; small fixes need only a test and code (CONTRIBUTING).

## D8. Freeze point and upstream flow — 2026-11-27

The port starts from upstream `main` at `e77dc49` (2026-11-27). Upstream has 15 open spec changes; each one that lands later
is ported by hand as its own change here, until the company tool repository switches to the package. After the switch
the flow reverses: fixes land here first.

## D9. One command with subcommands — 2026-11-27

`chat-<x>` tools become `aimost <x>`: `link` (daemon), `dispatch` (daemon), `edge` (server side), `inbox`, `post`, `confirm`,
`activity`, `bot add`, `channel`, `thread`, `file`, `react`, `who`, `agents`, `setup`, `plugins`, `commands`. Config moves
from `~/.config/chat/` to `~/.config/aimost/`, state from `~/.local/state/chat-*` to `~/.local/state/aimost/`; env prefix
`CHAT_` becomes `AIMOST_` (`CHAT_CONFIRM` → `AIMOST_CONFIRM`, `CHAT_POST_NAME` → `AIMOST_ROLE`). Exit codes of `post` keep
upstream values; plugin checks use 20–39 (the MR-link check moves from 9 to 20). `aimost migrate` imports an upstream
config and state.

## D10. Tests on stdlib unittest; session mode is core — 2026-11-27

Tests use `unittest` (pytest runs them unchanged): no dev dependency needed to run the suite on a bare Python. Run:
`python3 -m unittest discover -s tests -t tests`. Every module test is checked against a mutation (break the code, the test
must fail) before the module counts as ported.

The upstream "session mode" (`chat|cli`, in flight upstream as part of a larger change at the freeze point, already in the
inbox code) is generic and goes into the core delivery spec; the rest of that upstream change (folder pins, compaction
modes) is workspace-specific and stays out.

## D11. Two test levels: fast fake, real disposable Mattermost — 2026-11-27

- **Fast** (`python3 -m unittest discover -s tests -t tests`): no network, no Docker; every commit.
- **Integration**: a disposable Mattermost from `tests/integration/compose.yaml` (same image as production use, tmpfs
  data, 127.0.0.1 only, its own small subnet, memory caps): `tests/integration/up.sh` creates team `it`, people `owner`
  (admin) and `colleague`, bots `deployer` and `relay`, and writes tokens to `it.env` (gitignored); tests that need it
  skip when it is down (`AIMOST_IT=0` forces skipping). `down.sh` wipes it. Never point tests at a live server.
- What only the real server proves: props replaced whole on patch, bot permissions, websocket events, and real button
  presses calling the edge (`POST /posts/<id>/actions/<action>` as a user).

## D12. Bots are created over REST, not on the chat server — 2026-11-27

Upstream `chat-bot-add` ran `mmctl --local` on the chat server over ssh. `aimost bot add` uses the REST API with a token of
a user allowed to create bots (`AIMOST_ADMIN_TOKEN` or `<config>/admin/<connection>`, 0600): no shell access to the
server, works with any hosting. A bot for another person is assigned to them and no token is issued by the admin — they
take one with `/bot-token` (commands spec).

## D13. The bridge heartbeat lives in the bot's preferences — 2026-11-27

The status spec first put the heartbeat into bot user props, but a bot may not patch its own user (403 on the real
server). The bridge writes the bot's own preference `aimost_heartbeat/state` (JSON: seen, role, state, unread, last turn);
the edge's bot (a system admin) reads it, other users cannot. The edge interprets its age ("bridge offline since …").

## D14. One registry per extension point; the bridge, the waiting list and plugins meet there — 2026-11-27

Phase 3 was ported in parallel: the bridge (`link`), "Waiting for you" (`waiting`), plugins, packaging. Where two of them
needed the same thing, one copy was kept:
- answer sinks — `plugins.ANSWER_SINKS` (a plugin registers `answer(...)` and optional `rows(...)`); `waiting` reads the
  sinks that are on for the connection;
- the per-connection context (bots, ids, owner, co-owners) — `linkctx`, used by `link` and `waiting`;
- answers from the edge's marks — only `waiting.on_post`; the bridge passes own-bot posts to it;
- 👀 on ack — `receipts`, registered in `inbox` (`inbox` cannot import `link`: cycle);
- background units of plugins — `reg.service(...)` → `install.SERVICES`, installed by `aimost setup`.

Routing follows the upstream oracle where the spec text differed: in someone else's thread a person who called the role
without an @ is not delivered. `CHANNEL_OWNERS` defaults to `top <channel> -> role`. The workspace-specific listener
registry was not ported.

## Open

- Python floor: 3.8 (current) or 3.10 (pattern matching, better typing) — decide at step 3.
