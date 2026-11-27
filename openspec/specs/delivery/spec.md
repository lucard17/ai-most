# delivery Specification

## Purpose
The bridge delivers posts of people and of other agents from Mattermost into the inboxes of agent sessions (Claude Code,
Codex) and wakes a session when it has something to read: a live one by a message, a closed one by resuming the same
session. The bridge runs on the owner's machine and talks to the chat server over outbound connections only.

Terms: **connection** — one chat server with its config (`~/.config/lmost/connections/<name>.env`); **role** — a named
agent session (the name set by `/rename`), one bot per role; **owner** — the person the connection belongs to (`OWNER`);
**inbox** — the per-role event queue; **event** — one inbox line with `kind`, sender, sender role, `via`, channel, thread, post.

## Requirements

### Requirement: A post reaches the role's inbox
The bridge SHALL put a post into a role's inbox if it is in a direct message with the role's bot, mentions the bot, is in a
thread where the bot has posted, or is a top-level post without mentions in a channel the role owns
(`CHANNEL_OWNER=<channel>:<role>`). A post by another bot MUST be delivered only when it mentions this role's bot directly,
and only with sender role `peer`. The bot's own posts, system posts, webhook posts and progress posts MUST NOT become events.

#### Scenario: a person replies in the bot's thread
- **WHEN** the role's bot has posted in a thread, and a person replies in that thread
- **THEN** the role's inbox gets a `message` event, and the post gets the 📬 reaction
Oracle: `chat-link.test.sh`

### Requirement: A channel owner gets talk addressed to the role, not talk between people
A mention of the connection owner or `@here` MUST NOT stop a top-level post from reaching the channel's owner role; a post
that mentions another person MUST NOT be given to the channel owner role — that is people talking to each other.

#### Scenario: "@owner, can you look?" in the role's channel
- **WHEN** in a channel owned by a role a post mentions only the connection owner
- **THEN** the post is in the channel owner role's inbox
Oracle: `chat-link.test.sh`

### Requirement: In someone else's thread — only the people who called the role
In its own thread and in threads of a channel it owns, a role SHALL get every post; in someone else's thread where the
role has posted, only posts of people who called the role in that thread. A role MUST NOT get every post of every thread
it once posted in. Manual override: `lmost inbox mute|follow <role> <thread>`.

#### Scenario: people talk in a foreign thread
- **WHEN** the role once answered in someone else's thread, and other people keep talking there
- **THEN** their posts do not reach the role's inbox
Oracle: `chat-link.test.sh`

### Requirement: "Not delivered" only where an agent was expected
⚠️ on an owner's post SHALL be set only when the post reached no inbox where an agent was expected (direct message, owned
channel, top level of a channel matching `AGENT_CHANNELS`, a thread where a bot posted), and only after a full pass over all
bots. A mistyped `@name` gets ⚠️ and a "not delivered" reply in the thread. ⚠️ MUST NOT appear on replies in people's
threads or on a post delivered by another bot.

#### Scenario: an owner's post reached nobody
- **WHEN** an owner's post in an owned channel reached no inbox, or mentions a non-existent `@name`
- **THEN** the post gets ⚠️; for a typo, "not delivered" in the thread
Oracle: `chat-link.test.sh`

### Requirement: A new post is not lost after a reaction
The bridge SHALL decide whether a post is new by its creation time against the previous pass, not by `update_at`: a
reaction by another bot MUST NOT count as an edit.

#### Scenario: two bots in one channel
- **WHEN** the first bot has put 📬 on a post, and the second bot passes the channel
- **THEN** the post mentioning the second bot is in its inbox
Oracle: `chat-link-multibot.test.sh`

### Requirement: The bridge survives a restart of the chat server
A connection whose chat server was down when the bridge started SHALL be retried, not dropped until a manual restart.

#### Scenario: the chat server comes up after the bridge
- **WHEN** the chat server is unreachable at bridge start and comes up later
- **THEN** the bridge connects without a restart
Oracle: `chat-link-restart.test.sh`

### Requirement: Agent loop guard
After six agent posts in a row in a thread without a person, the bridge MUST NOT deliver further posts of that thread and
SHALL post "⏸ waiting for a person" in it. In channels listed in `LOOP_FREE_CHANNELS` the guard MUST NOT apply.

Endless bot-to-bot talk MUST NOT wake roles.

#### Scenario: bots answer each other
- **WHEN** the seventh bot post in a row without a person appears in a thread
- **THEN** no delivery, "⏸ waiting for a person" in the thread
Oracle: `chat-loop-free.test.sh`

#### Scenario: an agents' work channel
- **WHEN** the same happens in a thread of a channel from `LOOP_FREE_CHANNELS`
- **THEN** all posts are delivered, no "⏸ waiting for a person"
Oracle: `chat-loop-free.test.sh`

### Requirement: Urgent mid-turn, the rest at the end of the turn
The delivery hook SHALL hand over answers to questions, messages and the owner's reactions after the next tool call, and
source events (e.g. GitLab) and the rest in one batch at the end of the turn. A peer's post MUST NOT be handed over in the
middle of an owner's turn — only a line "N more from peers". A subagent MUST NOT read the inbox.

A peer's post MUST NOT make an owner's turn read-only: it is handled after the turn, and only the turn that handles it is
read-only.

#### Scenario: an answer arrives while the session works
- **WHEN** the session is working, and an `answer` arrives in its inbox
- **THEN** it is in the session's context after the next tool call
Test: `tests/test_hooks.py` (InboxHook)

### Requirement: Session mode — chat or cli
A session's mode SHALL be `chat` (the inbox wakes it and delivers mid-turn, questions go to chat) or `cli` (cut off from the
inbox: never woken, events wait for `/inbox` typed by the owner, questions in the terminal). An explicit label
(`lmost inbox mode <role> chat|cli`) SHALL win; without a label — `chat` if the role is in `WAKE_LIST` or has its own bot,
otherwise `cli`. A role, background or Codex session SHALL label itself `chat` on its first event if unlabelled
(`--if-unset`); a launch MUST NOT override an explicit label. In `cli` mode the dispatcher MUST NOT wake the session, the
delivery hook MUST NOT deliver on PostToolUse nor block Stop and SHALL show the owner "📬 N new in inbox — /inbox";
`lmost inbox put` SHALL store the event and tell the sender the session answers on `/inbox`; the bridge SHALL react ⌨️
instead of 📬. The peer guard MUST act in `cli` as in `chat`.

#### Scenario: a session started by hand
- **WHEN** the owner starts `claude`, names the session `foo`, it has no bot and is not in `WAKE_LIST`
- **THEN** `lmost inbox mode foo` prints `cli (default)`; no wake-ups
Test: `tests/test_inbox.py` (SessionMode)

#### Scenario: another session writes to a cli session
- **WHEN** a session calls `lmost inbox put foo --from hash "…"`, and `foo` is in `cli`
- **THEN** the event is stored, the sender sees "foo is in CLI mode — it answers when its owner runs /inbox", exit 0
Test: `tests/test_inbox.py` (SessionMode)

### Requirement: Waking an idle or closed session
The dispatcher SHALL wake a live idle session with an inter-session message "N in inbox — /inbox"; leave a busy one alone
if its transcript changed recently, otherwise wake it as idle; resume a closed role listed in `WAKE_LIST` with
`claude --bg --resume` of the same session. The dispatcher MUST NOT wake a session that waits for a person (a permission
prompt), and MUST NOT wake one session more often than once in 2 minutes.

#### Scenario: a session ended with unread events
- **WHEN** the session has ended, its inbox has unread events, nothing new arrives
- **THEN** it is resumed with `--resume`; a repeated resume — not more often than every 15 minutes
Test: `tests/test_dispatch.py` (Wake)

#### Scenario: "busy" after the turn ended
- **WHEN** `claude agents` shows the session busy, but its transcript has been silent for more than 2 minutes
- **THEN** the session is woken by a message
Test: `tests/test_dispatch.py` (Wake: busy), `tests/test_turn_origin.py` (TurnEnded)

### Requirement: A resumed session is the same session, with the owner's permissions
A resumed session SHALL run in its own directory, under its own name, in the owner's permission mode for sessions
(`LMOST_WAKE_ARGS`, empty by default). The dispatcher MUST NOT start a second copy of a live session; when there is nothing to
resume, it SHALL tell the owner in a direct message "nobody to wake" with the command to start the role
(`LMOST_WAKE_HINT`, configurable), instead of waking into the void.

#### Scenario: the role is not in the wake list
- **WHEN** a closed role has unread events, and it is not in `WAKE_LIST`
- **THEN** the owner gets "nobody to wake" with the start command in a direct message
Test: `tests/test_dispatch.py` (Wake)

### Requirement: The wake log records reading, not launching
The wake log SHALL record whether the inbox cursor moved ("read / NOT read within N s"). The exit code of a wake MUST NOT
count as success; the dispatcher MUST NOT retry a failed wake by itself.

#### Scenario: the wake did not land
- **WHEN** a session was woken, and its inbox cursor did not move within the check window
- **THEN** the log says "NOT read within N s", no retry
Test: `tests/test_dispatch.py` (Wake)

### Requirement: Shared bots and thread routing
Roles matching a pattern SHALL be able to post with a shared bot (`BOT_SHARED=<pattern>:<bot>`), and a post in a thread of
such a bot SHALL go to the role that owns that thread (`THREAD_ROUTE=<bot>=<command>`: the command maps the thread root
to a role) without waking the bot's main role.

#### Scenario: a person answers in a thread of a shared bot
- **WHEN** a person posts in a thread started by role `review-x` under the shared bot `review`
- **THEN** the event is in the inbox of `review-x`
Oracle: `chatlib-shared-bot.test.sh`, `chatlib.test.sh`

### Requirement: Relay bot for sessions without a bot
An admitted session without its own or a shared bot SHALL be able to post through the connection's relay bot (`RELAY`):
only into the owner's direct message, every post signed "from session <role>" in the first line. The relay MUST NOT post
into a channel or another person's DM (exit code 3), MUST NOT be used by a session that has a bot or is not admitted, and a
session MUST NOT edit, cancel or reply into another session's relay thread (exit code 3). The owner's reply, button answer
or reaction in a relay thread SHALL reach only the session that started the thread (`props.agent` of the root), with
sender role `owner`; a mention of the relay outside its threads reaches nobody.

#### Scenario: a session without a bot asks the owner
- **WHEN** session `probe` has no bot and calls `lmost post --ask …`
- **THEN** a question with buttons in the owner's DM from the relay, first line "from session probe"; the owner's press
  reaches `probe`'s inbox, not another session's
Oracle: `chat-relay.test.sh`; `tests/test_connections.py` (Bots)

### Requirement: Own connection and admission
A session admitted by several connections SHALL post to the default connection when `--via` is not given. A session that
is not admitted or has no bot MUST NOT post (exit code 3). Admission: `SESSIONS` (names or patterns), `SESSIONS_CWD`
(sessions started at or below a directory), and admission plugins (see extensions).

#### Scenario: a role on two servers
- **WHEN** a role is admitted to a personal and a work connection and posts without `--via`
- **THEN** the post goes to the default connection
Oracle: `chatlib-own-connection.test.sh`, `chatlib.test.sh`

### Requirement: Hooks stay silent where there is no chat
Hooks MAY be registered for everyone in a shared project; when the inbox directory does not exist they MUST exit at once,
print nothing and write nothing under `$HOME`.

Hooks MUST NOT look up the session through transcripts or create directories for people without the bridge; tests MUST NOT
write into the working tree.

#### Scenario: a colleague without the bridge
- **WHEN** the bridge is not installed, and any tool is called
- **THEN** hooks print nothing and create nothing
Test: `tests/test_hooks.py` (NoChat)

### Requirement: Codex uses the same inboxes
For Codex (hook input with `turn_id`) the role name SHALL come from `LMOST_ROLE`, and the inbox SHALL be handed over at
the start of the turn (UserPromptSubmit) and at its end (Stop).

An agent outside Claude Code SHALL be woken by its own command (`<role> cmd <command>` in `WAKE_LIST`: it gets the role,
the number of events and a prompt), not by a method built into the bridge. Where the hook format differs (PostToolUse in
Codex) events MUST NOT be swallowed silently.

#### Scenario: a Codex turn starts
- **WHEN** a role runs on Codex and starts a turn
- **THEN** unread inbox events are part of the turn's context
Test: `tests/test_hooks.py` (InboxHook: codex, ConfirmGuard: codex), `tests/test_dispatch.py` (OwnCommand)

### Requirement: Outbound connections only
The bridge SHALL run on the owner's machine and reach the chat server over outbound connections only (websocket and
polling; without a live websocket — polling every tick). Inbound connections from the chat server to the owner's machine
MUST NOT be required.

#### Scenario: no websocket
- **WHEN** the chat server does not serve the websocket
- **THEN** the bridge polls channels every tick and delivers posts
NEW (upstream has no regression).

### Requirement: The owner's reaction is an event
The owner's reaction on a role's post SHALL reach the role as a `reaction` event with meaning: 👍 — yes, ❌ — no,
👀 — seen.

#### Scenario: the owner puts 👍
- **WHEN** the owner puts 👍 on a role's post
- **THEN** the role's inbox gets a `reaction` event "yes"
Oracle: `chat-link.test.sh`
