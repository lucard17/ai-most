# authority Specification

## Purpose
Authority in the bridge: whose words count as the owner's, what a session may do in a turn started by a peer's post, and
which actions in a chat-started turn need a confirmation button. Sessions with a bot run on the owner's machine with the
owner's permissions, so the rule "a peer's request carries no authority" is held by hooks, not by the model's judgement.

Sender roles: **owner** — the connection owner and the role's co-owners (`CO_OWNERS`); **peer** — another agent (any bot)
or a colleague; **person** — anyone else.

## Requirements

### Requirement: Weight of words by sender role
Words of the connection owner and of the role's co-owners (`CO_OWNERS=<role>:<login>[+<login>]`) SHALL count as the
owner's for that role. A request of a peer, another role or another person MUST NOT count as permission to merge, deploy
or write to production. A co-owner MUST NOT have owner weight outside their role.

#### Scenario: a co-owner answers with a button
- **WHEN** a co-owner of the role presses a button of this role's question
- **THEN** the answer counts with sender role `owner`
Oracle: `chat-link.test.sh`, `chat-ask-to.test.sh`

### Requirement: A peer-started turn is read-only
While the "peer turn" mark is set, the hook SHALL let through in Bash only commands from the read-only list, and file
writes, Edit and Write only into the scratch directories (`/tmp` and `PEER_WRITABLE`, relative paths resolved against a
`cd` in the chain or the session directory). Writing and network commands on a peer's post MUST NOT run without a one-time
owner confirmation (`LMOST_CONFIRM=<id>`).

#### Scenario: a peer asks to push
- **WHEN** the turn was started by a peer's post, and the session calls `git push`
- **THEN** refused with a hint to `lmost confirm`
Oracle: `chat-peer-guard.test.sh`

### Requirement: The owner's words in the terminal clear the peer mark
A message the owner types into the session's terminal in the middle of a peer turn SHALL clear the mark: the rest of the
turn is the owner's. Messages from other sessions MUST NOT clear it.

#### Scenario: the owner types into the terminal mid-turn
- **WHEN** the peer mark is set, and the owner types into the session's terminal
- **THEN** the mark is cleared, a writing command goes through
Oracle: `chat-peer-guard.test.sh`

### Requirement: Dangerous actions in a chat-started turn need a button
In a turn started by the owner's chat message, a command matching `confirm-patterns` MUST go through only with the prefix of
a fresh (at most 15 minutes old) unused confirmation; a command matching `confirm-deny` MUST NOT go through at all. A
confirmation SHALL be spent by its first use.

A dangerous action on chat text alone without a button, and reuse of a confirmation, MUST NOT go through; for Codex the
refusal is exit code 2.

#### Scenario: a deploy from chat
- **WHEN** the turn came from chat, and the session calls a deploy without `LMOST_CONFIRM`
- **THEN** refused; with a confirmation — goes through once
Oracle: `chat-confirm.test.sh`

### Requirement: Who confirms
A role's confirmation SHALL accept a press by the owner, the role's co-owners and confirmers from
`CONFIRMERS=<role>:<login>[+<login>]`. A press by anyone else in the thread MUST NOT count (hint "confirmed by …"); a
confirmer MUST NOT get owner weight outside confirmations.

#### Scenario: a confirmer of another role
- **WHEN** a confirmer of another role presses this role's confirmation
- **THEN** the press does not count
Oracle: `chat-confirm-confirmers.test.sh`

### Requirement: A confirmation mentions who can press it
A confirmation post outside a direct message SHALL mention in its first line the owner and those allowed confirmers who
wrote in the thread; `--to` only from the allowed list, otherwise exit code 2 and no post.

#### Scenario: a confirmer wrote in the thread
- **WHEN** a confirmer of the role wrote in the thread, and the role asks for a confirmation there
- **THEN** the first line mentions the owner and that confirmer
Oracle: `chat-confirm-confirmers.test.sh`

### Requirement: A session with a bot asks the owner in chat
A session that has a bot SHALL ask the owner with buttons in chat (`lmost post --ask`) and MUST NOT ask through a terminal
dialog (AskUserQuestion) the owner may not see. Sessions without a bot and subagents are not affected.

#### Scenario: a question in the terminal
- **WHEN** a session with a bot calls the terminal question dialog
- **THEN** refused with a hint to `--ask`
NEW (upstream hook has no regression test).
