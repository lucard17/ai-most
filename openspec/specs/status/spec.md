# status Specification

## Purpose
From the chat one can see that an agent is alive and what it is doing: whether a message arrived, was read, whether a turn
is running, whether the session waits for a permission. Everything is set by the bridge, hooks and dispatcher; the session
does nothing for it.

## Requirements

### Requirement: Alive and busy is visible
In a chat-started turn the bridge SHALL show "typing…" in the thread, a progress post — only in a direct message, the bot's
state — as the user status and reactions 🔨/✋/✅ on the post that started the turn. The bridge MUST NOT change the bot's
avatar. Progress posts MUST NOT appear in channel threads; turn reactions MUST NOT be removed.

#### Scenario: a long turn from a direct message
- **WHEN** the owner wrote in a direct message, and the turn lasts more than 20 seconds
- **THEN** a progress post with the latest steps, at the end — the outcome
Test: `tests/test_activity.py`

### Requirement: A session waiting for a permission is visible to the owner
A session stopped on a permission prompt SHALL be shown as a separate state ✋, and after 120 s — with one direct message
to the owner (repeated at most every 30 minutes). A waiting session MUST NOT be shown as "not running"; the notice MUST NOT
go to a shared channel.

#### Scenario: the session stopped on a prompt
- **WHEN** the session waits for a permission for more than 120 seconds
- **THEN** the owner gets one direct message; the same wait is not reported again
Test: `tests/test_activity.py`

### Requirement: Bridge heartbeat
The bridge SHALL write a heartbeat at least once a minute into a place the edge can read without the owner's machine (the
props of each role bot: `lmost_seen` — time, `lmost_state` — running / idle / waiting / not running, unread count, last turn
time). A missing heartbeat MUST NOT change the bots' visible status by itself; the edge interprets its age.

#### Scenario: the machine goes to sleep
- **WHEN** the bridge stops at 14:00
- **THEN** `lmost_seen` stays 14:00; the edge reports "bridge offline since 14:00" from 14:03 on
NEW

### Requirement: Receipts by reactions
The addressee's bot SHALL put 📬 when the bridge put the post into the inbox, 👀 when the session read it, ⚠️ — not
delivered. A failing receipt MUST NOT break delivery.

#### Scenario: a post delivered and read
- **WHEN** the post is in the inbox, and the session has read it
- **THEN** the post has 📬 and 👀 from the addressee's bot
Oracle: `chat-link-multibot.test.sh`

### Requirement: One bot per role
Every role SHALL have its own bot and SHALL post with its token: a person's bot is `<OWNER>-<role>` or a short prefix of the
person's choice, shared bots have no prefix; without `OWNER`, `lmost bot add` refuses. Posting for a role with another
bot's token or through a webhook with a substituted name MUST NOT happen.

#### Scenario: the role's bot is deactivated
- **WHEN** `lmost bot add` for a role whose bot is deactivated
- **THEN** the bot is enabled, the token is new and written to a file, not printed
Test: `tests/test_botadmin.py`

### Requirement: Bots for other people are created by a chat admin
Bots for other people SHALL be created by a chat admin (`lmost bot add --owner <login>`, description required): the bot is
assigned to that person, no token is issued by the admin — the owner takes one with `/bot-token <bot>` (commands spec) —
and the admin's own connection is untouched. An `--owner` that is a bot or does not exist (exit code 6), silently moving
a bot owned by someone else (exit code 7), self-service accounts, and turning a person's account into a bot (exit code 4)
MUST NOT happen.

#### Scenario: a person's login instead of a bot name
- **WHEN** the admin creates a bot with a name taken by a person's account
- **THEN** refused, exit code 4
Test: `tests/test_botadmin.py`

### Requirement: The agent directory is built from bot profiles
The agent directory SHALL be built from bot profile descriptions (deleted bots excluded) and published as a pinned post
edited in place; a hand-kept list of agents MUST NOT exist.

#### Scenario: nothing changed
- **WHEN** the directory is published, and no profile changed
- **THEN** nothing is written
Test: `tests/test_agents.py`
