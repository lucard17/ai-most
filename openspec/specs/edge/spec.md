# edge Specification

## Purpose
Mattermost buttons, dialogs and slash commands call a URL the chat server can reach, and the owner's machine is not
reachable from the server. So the endpoint — `aimost edge` — runs next to the chat server (a container in its compose). It
only records the choice in the properties of the post itself; what counts is decided by the owner's bridge, which sees the
change over its outbound connection.

## Requirements

### Requirement: The edge records presses next to the server
The edge SHALL record a choice in the properties of the question or confirmation post (`props.aimost_answers`); it SHALL
take the addressees from the button context and MUST NOT record a press by a non-addressee. Whether an answer counts SHALL
be decided by the owner's bridge.

#### Scenario: an addressee presses
- **WHEN** an addressee of a question presses a button
- **THEN** the post shows the choice, the bridge puts an `answer` into the asking session's inbox
Test: `test_edge.Addressees`, `test_edge_it.RealEdge.test_a_person_presses_a_button` (oracle `chat-ask-to.test.sh`,
`chat-confirm-confirmers.test.sh`)

### Requirement: Buttons are signed with the bot owner's key
Every button context SHALL carry the key of the bots' owner (`EDGE_SECRET`, identified by `EDGE_KEY_ID`). Owner keys SHALL
be derived by the edge from its master key, owner id and generation (`EDGE_MASTER`); the edge keeps a keys file with owner,
generation and revocation, without secrets. A key MUST be accepted only on posts of its owner's bots; a revoked key, a key of
another owner and a broken keys file MUST make the edge refuse buttons with that key id. Keys are issued and revoked with
the `/bot-token key` slash command.

#### Scenario: a key used on another owner's bot
- **WHEN** a button on a post of owner B's bot carries owner A's key
- **THEN** refused, the post is unchanged
Test: `test_edge.OwnerKeys`, `test_edge.Dialog`, `test_edge_it.RealEdge.test_owner_key_signs_buttons` (oracle
`chat-edge-keys.test.sh`, `chat-edge-dialog-state.test.sh`)

### Requirement: Slash command URLs are keyed
Each slash command SHALL be registered in Mattermost with the URL `/<command>?k=<hmac(EDGE_SLASH_SECRET, command)>`; the
edge MUST answer 403 to a call with a wrong key — its port is visible to the chat server only, the key stops other callers.

#### Scenario: a call without the key
- **WHEN** a request reaches `/unfollow` without a valid `k`
- **THEN** 403, nothing changes
Test: `test_edge.Http`, `test_commands.Registry.test_registration_urls_are_keyed` (oracle `chat-unfollow.test.sh`)

### Requirement: The bridge notices a stale edge
The edge SHALL report its version; the bridge SHALL warn the owner once per version when the edge version differs from its
own. A changed edge MUST NOT be assumed live before it re-executes: the edge checks its package files between requests
(`EDGE_RELOAD_S`) and re-executes in place on a change; code that does not compile is not run (one log line, the previous
code keeps serving).

#### Scenario: the bridge was upgraded, the edge was not
- **WHEN** the bridge runs version N+1, and the edge reports N
- **THEN** the owner gets one direct message: the edge is stale, restart it
Edge side (`GET /version`, re-exec): `test_edge.Http`, `test_edge.Reload` (oracle `chat-edge-reload.test.sh`); the
bridge warning — NEW, lands with the bridge.

### Requirement: One props prefix per chat server
The names ai-most leaves in the chat (post props, preference categories) SHALL be `<prefix>_<name>` with the prefix
`aimost` unless set (`AIMOST_PROPS_PREFIX`, or `<config>/props-prefix` on the owner's machine), so that a server whose
posts an earlier bridge version wrote under another prefix keeps working. Behaviour MUST NOT depend on the prefix. The
edge SHALL report its prefix in `GET /version`; a bad prefix SHALL stop the process before it touches the chat.

#### Scenario: a team moving from the earlier bridge
- **WHEN** the bridge and the edge run with prefix `chat`
- **THEN** buttons of posts the earlier bridge wrote are answered, and every behaviour suite passes with `chat_*` names
Test: `tests/test_names.py`
