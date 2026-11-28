# admin Specification

## Purpose
Admin actions in the chat — deleting posts, system messages, sidebar categories for people, channel members, role bots —
without an admin password or token on any owner's machine. A role only ASKS for an action; the edge on the chat server
(already a system admin) makes a dry run, posts the plan with buttons, and executes only after a confirmer's press on that
post. It is the optional built-in plugin `aimost_admin` (off unless `AIMOST_PLUGINS=admin` / `PLUGINS=admin`).

Upstream: the working version's `chat-admin` role (after the D8 freeze point), ported with its company-specific parts as
settings (D18).

## Requirements

### Requirement: The edge executes, the owner's machine only asks
Admin actions SHALL be executed only by the edge with its own bot. The chat address, the confirmers and the admin button
secret SHALL come from the edge's environment (`EDGE_ADMIN_SECRET`, `EDGE_ADMIN_CONFIRMERS`), never from the caller's
settings. `aimost admin request --spec <file>|-` SHALL check the form by the same rules as the edge, post the request with
props `<prefix>_admin` {action, target, why, origin} as the admin role's bot into the log channel, and call
`/<command> <post id>` with that bot (`AIMOST_ADMIN_COMMAND`, default `admin`).

#### Scenario: a request from the owner's machine
- **WHEN** the admin role posts a request and calls the command
- **THEN** the edge posts a dry run with buttons; nothing is executed on the owner's machine
Test: `tests/test_admin.py` (Flow, Requester.test_form_and_send)

### Requirement: A closed action list and an API path list
The edge SHALL execute only `posts-delete`, `system-messages-delete`, `sidebar-category`, `channel-member-add`,
`channel-member-remove`, `bot-create`, `bot-enable` and rollbacks of their results. Anything else, a post in a DM or group
message, adding someone to a private channel (`channel-member-add`, the channels of `bot-create` — "ask a member of that
channel"), enabling a bot whose owner is not the edge owner (`EDGE_OWNER`; the owner field of a request does not count),
SHALL end in a refusal with a journal line and no write. A request outside the list of API methods and paths SHALL be
refused before sending; `permanent=true` only for the two delete actions. Who may ask for what — `EDGE_ADMIN_ORIGINS`
(`<action>=<origin>|…;…`, `*` for all), by default the edge owner only.

#### Scenario: a request to grant roles
- **WHEN** a request has `action: role-grant`
- **THEN** "is not in the list", no buttons, a `refused` journal line
Test: `tests/test_admin.py` (Refusals.test_closed_list_and_form, Refusals.test_dm_private_channels_and_foreign_bots,
Refusals.test_api_paths, Refusals.test_origins_setting)

### Requirement: The confirmer's press on its own post decides
The confirmation post SHALL show the plan first (channels and people by name, counts, the first targets), then the
rollback or **irreversible**, the fingerprint (sha256 of the normalized target and what it resolved to), who asks and why
(one line in code). Its buttons SHALL be signed with `EDGE_ADMIN_SECRET`, which MUST differ from every other edge key.
Execution SHALL happen only on a press by a confirmer, on that very post (its fingerprint and nonce equal the button's),
within 15 minutes, once, and only if a second dry run gives the same fingerprint. Only the admin role's bot
(`EDGE_ADMIN_REQUESTER`) calls the command, with the command token (`EDGE_ADMIN_CMD_TOKEN`); the request and the
confirmation live in the private log channel (`EDGE_ADMIN_LOG_CHANNEL`).

#### Scenario: the request changed after the post
- **WHEN** the targets changed after the dry run, then the confirmer presses
- **THEN** "the fingerprint does not match", nothing is touched
Test: `tests/test_admin.py` (Refusals.test_press_checks, Refusals.test_who_calls_and_where, Refusals.test_old_press_and_no,
Refusals.test_secret_must_be_its_own)

### Requirement: A copy before, a check after, a rollback
Before writing, the edge SHALL save what changes into its state (`EDGE_ADMIN_STATE`); deleting posts — the copy first,
no copy — no delete, permanently afterwards, no rollback ("irreversible"); the posts of the log channel and of the edge are
never deleted. After writing it SHALL check by reading again and record `done` / `partial N of M` / `error` in the post, the
journal and the log channel. A rollback SHALL go the same way, within 24 h, once, never into a private channel, and remove
only what the edge added (a person's own categories and channels stay). A failure midway SHALL still record the result
and the rollback data of what was done; without the `started` journal line nothing starts.

#### Scenario: delete 3 posts
- **WHEN** a request to delete a root (with a reply) and another post, the confirmer presses
- **THEN** the post says **irreversible** before the press; the copy of 3 posts is in the state; all 3 are gone; "done 3 of 3"
Test: `tests/test_admin.py` (Flow, Refusals.test_rollback_limits, Refusals.test_audit_posts_are_not_deleted)

### Requirement: Role bots through the edge
Without an admin token, `aimost bot add` SHALL ask for `bot-create` (owner — `--owner` or the connection's `OWNER`) or, with
`--enable`, for `bot-enable`; nobody issues a token for the owner — they take it with `/bot-token`, put it into the token
file (0600) and run `aimost bot add <role> --admit`. `--enable --owner` SHALL be refused.

#### Scenario: the role's bot is disabled
- **WHEN** `aimost bot add <role> --enable` and the confirmer presses
- **THEN** its live tokens are revoked first, the bot is enabled; then `/bot-token` and `--admit`
Test: `tests/test_admin.py` (Flow.test_bot_create_and_enable, Requester.test_bot_add_without_an_admin_token_asks)
