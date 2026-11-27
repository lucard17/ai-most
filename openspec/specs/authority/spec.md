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
writes, Edit and Write only into the scratch directories (`/tmp` or `AIMOST_PEER_TMP`, and `PEER_WRITABLE` — `AIMOST_PEER_WRITABLE` and
`<config>/peer-writable`, relative paths resolved against a
`cd` in the chain or the session directory). Writing and network commands on a peer's post MUST NOT run without a one-time
owner confirmation (`AIMOST_CONFIRM=<id>`).

#### Scenario: a peer asks to push
- **WHEN** the turn was started by a peer's post, and the session calls `git push`
- **THEN** refused with a hint to `aimost confirm`
Test: `tests/test_hooks.py` (PeerGuard); oracle `chat-peer-guard.test.sh`

### Requirement: The owner's words in the terminal clear the peer mark
A message the owner types into the session's terminal in the middle of a peer turn SHALL clear the mark: the rest of the
turn is the owner's. Messages from other sessions MUST NOT clear it.

#### Scenario: the owner types into the terminal mid-turn
- **WHEN** the peer mark is set, and the owner types into the session's terminal
- **THEN** the mark is cleared, a writing command goes through
Test: `tests/test_hooks.py` (PeerGuard: owner typing, owner turn after the mark), `tests/test_turn_origin.py`

### Requirement: Dangerous actions in a chat-started turn need a button
In a turn started by the owner's chat message, a command matching `confirm-patterns` MUST go through only with the prefix of
a fresh (at most 15 minutes old) unused confirmation; a command matching `confirm-deny` MUST NOT go through at all. A
confirmation SHALL be spent by its first use.

A dangerous action on chat text alone without a button, and reuse of a confirmation, MUST NOT go through; for Codex the
refusal is exit code 2.

#### Scenario: a deploy from chat
- **WHEN** the turn came from chat, and the session calls a deploy without `AIMOST_CONFIRM`
- **THEN** refused; with a confirmation — goes through once
Test: `tests/test_hooks.py` (ConfirmGuard), `tests/test_confirm.py` (Records)

### Requirement: A confirmation describes the action
`aimost confirm` SHALL take the action as fields (`--spec <action.json>`): what (`do`), why and at whose request (`why`),
what happens step by step (`steps`, 1–7), what stays the same (`keeps`), how to undo it or why it cannot be undone
(`rollback`), and optionally the merge requests it is about (`mrs`), the ones it merges (`merges`) and the commands
(`commands`, kept with the confirmation, never posted). A missing field, a bare shell command in a meaning field, an
unknown field, or a merge request in `merges` that is already merged or closed MUST be refused with exit code 4 before
anything is posted; the two-free-strings form MUST be refused with exit code 2. The post SHALL show the fields in a fixed
template, a card per merge request (title, state, CI on the head commit — from a plugin, otherwise "not checked") and a
rule line that promises a merge only for the merge requests in `merges`. On a press the edge SHALL keep the text the
presser saw and add the outcome. A merge command under a confirmation (`glab mr merge`, `gh pr merge`) SHALL go through
only when the confirmation names that merge request in `merges`; a merge request only in a card or a step is not covered.

#### Scenario: a "yes" to closing a review is not a "yes" to a merge
- **WHEN** the owner confirmed "close the review" whose card shows `api!46`, and the session runs
  `AIMOST_CONFIRM=<id> glab mr merge 46 -R group/api`
- **THEN** refused: the confirmation does not name `api!46` in `merges`; the confirmation is not spent
Oracle: upstream `chat-confirm-template.test.sh`, `chat-confirm.test.sh`. Test: `tests/test_confirm_spec.py`,
`tests/test_hooks.py` (ConfirmGuard.test_a_merge_only_for_the_merge_requests_in_merges, PeerGuard), `tests/test_confirm.py`
(RealServer), `tests/test_edge.py` (test_confirm_described_by_fields_keeps_the_text), `tests/test_gitlab.py` (Cards)

### Requirement: Who confirms
A role's confirmation SHALL accept a press by the owner, the role's co-owners and confirmers from
`CONFIRMERS=<role>:<login>[+<login>]`. A press by anyone else in the thread MUST NOT count (hint "confirmed by …"); a
confirmer MUST NOT get owner weight outside confirmations.

#### Scenario: a confirmer of another role
- **WHEN** a confirmer of another role presses this role's confirmation
- **THEN** the press does not count
Test: `tests/test_confirm.py` (RealServer, disposable Mattermost)

### Requirement: A confirmation mentions who can press it
A confirmation post outside a direct message SHALL mention in its first line the owner and those allowed confirmers who
wrote in the thread; `--to` only from the allowed list, otherwise exit code 2 and no post.

#### Scenario: a confirmer wrote in the thread
- **WHEN** a confirmer of the role wrote in the thread, and the role asks for a confirmation there
- **THEN** the first line mentions the owner and that confirmer
Test: `tests/test_confirm.py` (RealServer)

### Requirement: A session with a bot asks the owner in chat
A session that has a bot SHALL ask the owner with buttons in chat (`aimost post --ask`) and MUST NOT ask through a terminal
dialog (AskUserQuestion) the owner may not see. Sessions without a bot and subagents are not affected.

#### Scenario: a question in the terminal
- **WHEN** a session with a bot calls the terminal question dialog
- **THEN** refused with a hint to `--ask`
Test: `tests/test_hooks.py` (AskGuard)
