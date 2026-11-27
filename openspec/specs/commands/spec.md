# commands Specification

## Purpose
Mattermost slash commands (`/agent status`, `/unfollow @bot 2h`) give people a short way to talk to the bridge and to agent
roles. Mattermost calls a URL reachable from the chat server, so every command enters through the edge; the owner's machine
is reachable only through its own outbound connection. Hence a command runs in one of three places, chosen by its
**kind**:

| kind | runs | sees | examples |
| --- | --- | --- | --- |
| `edge` | in the edge, next to the server | chat API with the edge's bot, bots' status and heartbeat | `/unfollow`, `/bot-token`, `/agent status`, `/agent who` |
| `bridge` | in the owner's bridge | the owner's inboxes, registry, sessions | `/agent waiting`, `/agent mute`, `/agent follow` |
| `session` | in the role's session, as an inbox event | everything the session sees | `/agent ask deployer "…"` |

## Requirements

### Requirement: A command registry
Commands SHALL be declared in a registry: name, subcommand, arguments, help line, kind, and who may call
(`owner` / `peer` / `person`, by default `owner`). At the edge the caller is the `owner` of a bot it is the `owner_id` of,
a `peer` when it is a bot, otherwise a `person`; the bridge checks again with co-owners. Built-in commands and plugin
commands (see extensions) SHALL use the same registry. `aimost commands register` SHALL print (or, with an admin token, create) the Mattermost slash commands with keyed
URLs. `/<command> help` SHALL list only the subcommands the caller may use.

#### Scenario: help for a person who is not an owner
- **WHEN** a person without owner rights calls `/agent help`
- **THEN** only subcommands open to `person` are listed
Test: `test_commands.Registry`, `test_edge_it.RealEdge.test_slash_commands_through_mattermost`

### Requirement: Authority is checked by the core, not by the command
The caller's sender role SHALL be resolved by the same rules as for posts (authority spec) before the handler runs; a
handler MUST NOT be called for a caller without the required role. A `session` command from a non-owner SHALL arrive as a
`peer`/`person` event and start a read-only turn; a command MUST NOT be a way around confirmation or the peer read-only rule.

#### Scenario: a colleague tries a session command
- **WHEN** a colleague calls `/agent ask deployer "deploy now"`, and the command is open to `person`
- **THEN** the event arrives with sender role `person`; the turn is read-only; a deploy needs the owner's confirmation
Test (edge side): `test_commands.Authority`; the read-only turn — with the bridge (NEW)

### Requirement: Edge commands answer at once
An `edge` command SHALL answer the caller with an ephemeral response within Mattermost's timeout; on a handler error the
caller SHALL get "did not work — try again later" and the edge log the error type without user data.

#### Scenario: unfollow settings
- **WHEN** a person calls `/unfollow @bot 2h`
- **THEN** an ephemeral confirmation; threads of that bot silent for 2 hours are unfollowed for that person
Test: `test_commands.Unfollow`, `test_commands.BotToken`, `test_edge.Http.test_handler_error_is_generic`
(oracle `chat-unfollow.test.sh`, `chat-bot-token.test.sh`)

### Requirement: Bridge and session commands travel as command posts
For `bridge` and `session` kinds the edge SHALL record the command as a post in the direct message between the caller and
the target bot (props `aimost_command`: name, args, caller, signed with the bot owner's key) and answer ephemerally
"sent to @<bot>". The post is written by the edge's bot and carries `caller`, the target `bot`, `team_id`, `channel_id`
and `root_id` of the call, and `kid` when signed with a per-owner key; the sender role is not in it — the bridge resolves it.
A command that names no role (`/agent waiting`) goes to the caller's bot with the freshest heartbeat. The owner's bridge
SHALL pick it up over its outbound connection, verify the key, check authority, and either run the `bridge` handler and reply in that DM thread, or put a `command` event into the role's inbox. A command post
with a wrong key MUST be ignored and logged.

#### Scenario: mute a role in a thread
- **WHEN** the owner calls `/agent mute deployer` in a thread
- **THEN** a command post in the owner's DM with the bot; the bridge mutes the thread for that role and replies in the
  command post's thread
Test (edge side): `test_commands.CommandPosts`, `test_edge_it.RealEdge.test_session_command_becomes_a_signed_post`;
the bridge side — NEW

#### Scenario: the owner's machine is offline
- **WHEN** the bridge's heartbeat is older than 3 minutes, and a `bridge` or `session` command is called
- **THEN** the ephemeral answer says "bridge offline since <time> — will deliver when it is back"; the command post stays
Test: `test_commands.CommandPosts.test_bridge_offline`

#### Scenario: a stale command when the bridge comes back
- **WHEN** the bridge comes back and finds a command post older than `COMMAND_TTL` (10 min by default)
- **THEN** it does not run it; it asks the caller in the post's thread with buttons "the command is <age> old — still
  needed? yes / no"; "yes" runs it as if new, "no" marks it dropped; a non-caller's press does not count
NEW (bridge side)

### Requirement: Status and who are answered by the edge itself
`/agent status [role]` and `/agent who` SHALL be `edge` commands: the edge answers at once from the bots' status and the
bridge heartbeat (status spec), without the owner's machine. The answer SHALL show the data age, and when the heartbeat is
older than 3 minutes — "bridge offline since <time>" with the last known state of each role.

#### Scenario: status from the phone, machine on
- **WHEN** the owner calls `/agent status deployer`, and the heartbeat is 30 s old
- **THEN** an ephemeral answer at once: state, unread count, last turn time, "data 30 s old"
Test: `test_commands.StatusWho.test_fresh_heartbeat`

#### Scenario: status from the phone, machine off
- **WHEN** the owner calls `/agent who`, and the last heartbeat was at 14:00
- **THEN** an ephemeral answer at once: "bridge offline since 14:00" and the last known state of every role
Test: `test_commands.StatusWho.test_machine_off`, `test_offline_after_three_minutes`

### Requirement: Built-in commands of v1
v1 SHALL ship: `edge` — `/unfollow`, `/bot-token`, `/agent status [role]`, `/agent who`; `bridge` — `/agent waiting`
(the "Waiting for you" list), `/agent mute|follow <role>` in the current thread; `session` — `/agent ask <role> "<text>"`.

#### Scenario: list of roles
- **WHEN** the owner calls `/agent who`
- **THEN** the reply lists the owner's roles with state and unread count
Test: `test_commands.StatusWho`, `test_commands.Registry.test_v1_commands`
