# commands Specification

## Purpose
Mattermost slash commands (`/agent status`, `/unfollow @bot 2h`) give people a short way to talk to the bridge and to agent
roles. Mattermost calls a URL reachable from the chat server, so every command enters through the edge; the owner's machine
is reachable only through its own outbound connection. Hence a command runs in one of three places, chosen by its
**kind**:

| kind | runs | sees | examples |
| --- | --- | --- | --- |
| `edge` | in the edge, next to the server | chat API with the edge's bot | `/unfollow`, `/bot-token` |
| `bridge` | in the owner's bridge | the owner's inboxes, registry, sessions | `/agent status`, `/agent who`, `/agent snooze` |
| `session` | in the role's session, as an inbox event | everything the session sees | `/agent ask deployer "…"` |

## Requirements

### Requirement: A command registry
Commands SHALL be declared in a registry: name, subcommand, arguments, help line, kind, and who may call
(`owner` / `peer` / `person`, by default `owner`). Built-in commands and plugin commands (see extensions) SHALL use the same
registry. `lmost commands register` SHALL print (or, with an admin token, create) the Mattermost slash commands with keyed
URLs. `/<command> help` SHALL list only the subcommands the caller may use.

#### Scenario: help for a person who is not an owner
- **WHEN** a person without owner rights calls `/agent help`
- **THEN** only subcommands open to `person` are listed
NEW

### Requirement: Authority is checked by the core, not by the command
The caller's sender role SHALL be resolved by the same rules as for posts (authority spec) before the handler runs; a
handler MUST NOT be called for a caller without the required role. A `session` command from a non-owner SHALL arrive as a
`peer`/`person` event and start a read-only turn; a command MUST NOT be a way around confirmation or the peer read-only rule.

#### Scenario: a colleague tries a session command
- **WHEN** a colleague calls `/agent ask deployer "deploy now"`, and the command is open to `person`
- **THEN** the event arrives with sender role `person`; the turn is read-only; a deploy needs the owner's confirmation
NEW

### Requirement: Edge commands answer at once
An `edge` command SHALL answer the caller with an ephemeral response within Mattermost's timeout; on a handler error the
caller SHALL get "did not work — try again later" and the edge log the error type without user data.

#### Scenario: unfollow settings
- **WHEN** a person calls `/unfollow @bot 2h`
- **THEN** an ephemeral confirmation; threads of that bot silent for 2 hours are unfollowed for that person
Oracle: `chat-unfollow.test.sh`

### Requirement: Bridge and session commands travel as command posts
For `bridge` and `session` kinds the edge SHALL record the command as a post in the direct message between the caller and
the target bot (props `lmost_command`: name, args, caller, signed with the bot owner's key) and answer ephemerally
"sent to @<bot>". The owner's bridge SHALL pick it up over its outbound connection, verify the key, check authority, and
either run the `bridge` handler and reply in that DM thread, or put a `command` event into the role's inbox. A command post
with a wrong key MUST be ignored and logged.

#### Scenario: status of a role from the phone
- **WHEN** the owner calls `/agent status deployer` in the mobile app
- **THEN** a command post in the owner's DM with the bot; the bridge replies in its thread: running / idle / waiting ✋,
  unread count, last turn time
NEW

#### Scenario: the owner's machine is offline
- **WHEN** the owner's bridge is not running, and a `bridge` command is called
- **THEN** the ephemeral answer says "sent", the command post stays; when the bridge comes back it handles commands not
  older than `COMMAND_TTL` (10 min by default) and marks older ones "expired"
NEW

### Requirement: Built-in commands of v1
v1 SHALL ship: `edge` — `/unfollow`, `/bot-token`; `bridge` — `/agent status [role]`, `/agent who`, `/agent waiting`
(the "Waiting for you" list), `/agent mute|follow <role>` in the current thread; `session` — `/agent ask <role> "<text>"`.

#### Scenario: list of roles
- **WHEN** the owner calls `/agent who`
- **THEN** the reply lists the owner's roles with state and unread count
NEW
