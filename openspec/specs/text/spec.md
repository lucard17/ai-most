# text Specification

## Purpose
What a session writes to people in chat: understandable without the session's context, with mentions that actually
notify, nothing from the owner's private life in a work chat. Checks run in `aimost post` before sending and form a pipeline
of text checks (built-in ones below, more from plugins — see extensions); a refusal is an exit code with a hint.

## Requirements

### Requirement: The owner's private matters stay out of a work chat
On a connection with `PRIVATE_GUARD=on` everything that goes to chat (post text and edits, pins, channel header and
purpose, question descriptions, attachment names and text, the agent directory) SHALL be checked against the private
patterns (`private-patterns`); a match MUST give exit code 5, a missing patterns file — also a refusal.

#### Scenario: a post about a private project
- **WHEN** the post text matches a private pattern
- **THEN** exit code 5, nothing is posted
Test: `tests/test_textcheck.py` (PrivateGuard), `tests/test_post.py` (Basics.test_private_guard)

#### Scenario: private text in an edit or the directory
- **WHEN** the private text is in a post edit or in the agent directory
- **THEN** also exit code 5
Test: `tests/test_post.py` (Basics.test_private_guard — edit, question), `tests/test_chat_tools.py` (Channel — header); agent directory — Oracle: `chat-agents.test.sh`

### Requirement: A post to people has structure
A post's own text, a question's context and an edit SHALL be refused with exit code 11 when the first line (after a line of
mentions only) is over 200 characters, a paragraph or a single line is over 350, or a text over 500 has no field, list item
or heading; code blocks, quote lines and link addresses do not count. The hint SHALL say the form: the point in the first
line, fields in bold each on its own line, lists as lists. `--plain`, a `.md` document sent as post text and a pin are not
checked; text a tool composes itself (the wake dispatcher's DM) goes with `--plain`.

#### Scenario: a wall of text
- **WHEN** a session posts one solid paragraph of 600 characters
- **THEN** refused with code 11 and the hint; the same text with `--plain` goes out
Oracle: upstream `chat-post-markup.test.sh`, `chat-dispatch-orphan-plain.test.sh`. Test: `tests/test_textcheck.py`
(Layout), `tests/test_post.py` (Basics.test_wall_of_text, real server), `tests/test_dispatch.py`
(test_nobody_to_wake_tells_the_owner_once)

### Requirement: Back-ticks in a post's text are not run by the shell
A command that passes post or merge request text to `aimost post|confirm|channel`, `glab … mr create|update|note` or
`gh … pr|issue create|edit|comment` SHALL be refused before it runs (hook `quote-guard`, PreToolUse Bash) when an unescaped
back-tick stands inside double quotes or in the body of a heredoc with an unquoted delimiter — the shell would run it and
put nothing in its place. Single quotes, an escaped back-tick, a heredoc with a quoted delimiter (also inside
`"$(cat <<'EOF' … EOF)"`), comments and other commands MUST pass. The hint SHALL show the heredoc form.

#### Scenario: logins in back-ticks
- **WHEN** a session runs ``aimost post --thread T "earlier `@ann` muted"``
- **THEN** refused with the heredoc hint; the same text through `<<'EOF'` goes out
Oracle: upstream `chat-shell-quote-guard.test.sh`. Test: `tests/test_hooks.py` (QuoteGuard), `tests/test_plugin.py`
(Manifest)

### Requirement: Logins and members are checked against the server
A non-existent `@login` in the text or in `--to` MUST give exit code 6 with similar logins; a mentioned person or addressee
not in the channel — exit code 7 (`--invite` SHALL add them); the bot itself not in the channel — exit code 8.

Mentions that notify nobody, and buttons for someone who will not see them, MUST NOT be sent; hint — `aimost who`.

#### Scenario: a login from another system
- **WHEN** a login that does not exist in chat is mentioned
- **THEN** exit code 6 and similar logins
Test: `tests/test_post.py` (Basics.test_unknown_login, Channels.test_members_invite_and_bot_not_in_channel, Asks.test_bot_addressee_and_non_member)

### Requirement: A document goes as post text
A `.md` file without post text SHALL be sent as the post text without an attachment (a long one — in parts into the
thread); as an attachment — only with `--attach`. Mermaid diagrams SHALL also be posted as an image into the thread for
phones.

The same message in several copies (text, attachment, path) MUST NOT be sent: a file or path only when it holds more than
the post.

#### Scenario: a report as a file
- **WHEN** `--file report.md` without text
- **THEN** one post with the text, no attachment
Test: `tests/test_post.py` (Files), `tests/test_textcheck.py` (MdChunks)

### Requirement: Editing own posts and pins
`--edit <post_id>` SHALL edit only the role's own post (someone else's — exit code 3) and only its text, with the same
checks as a new post. `--pin <key>` SHALL keep one pinned post per key and channel and edit it in place.

A channel is laid out as: purpose — one phrase, header — one line with links, rules — a pin by key (`aimost channel`,
`aimost post --pin`).

#### Scenario: channel rules updated
- **WHEN** a rules pin exists, and the role calls `--pin rules` with new text
- **THEN** the same post is edited, no new post
Test: `tests/test_post.py` (Channels.test_pin_edited_in_place, Channels.test_edit_own_only), `tests/test_chat_tools.py` (Channel)

### Requirement: Reply into the event's thread and the turn's thread
A session's reply SHALL go into the event's thread with `--via` from the event; to another role — by mentioning its bot in
the thread. During a chat-started turn a post without `--thread` SHALL go into that turn's thread; a new topic —
`--new-thread`. A conversation started in chat MUST NOT be moved to inter-session messages past the thread, and a reply
MUST NOT be scattered over new top-level threads.

#### Scenario: a reply during a turn from a direct message
- **WHEN** the turn was started by a direct message, and the session posts without `--thread`
- **THEN** the post goes into that turn's thread
Test: `tests/test_post.py` (TurnThread); the turn state itself — Oracle: `chat-activity.test.sh`
