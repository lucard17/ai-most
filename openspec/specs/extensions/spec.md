# extensions Specification

## Purpose
ai-most is a small core with extension points. Everything specific to one team — its task tracker, review flow, launcher,
text rules — lives in plugins, so the core stays usable by anyone. One pattern everywhere: a **registry** and an
**interface**; plugins are found through Python entry points (group `aimost.plugins`) and the local directory
`~/.config/aimost/plugins/`.

| Extension point | Interface (short) | Built-in |
| --- | --- | --- |
| `transport` | connect, read posts since, post/edit, buttons, reactions, users, websocket events | Mattermost |
| `source` | poll or listen, write typed events into a role's inbox | GitLab MR (gitlab-mr spec) |
| `delivery` | deliver inbox events into a running session; wake an idle or closed one | Claude Code hooks, Codex hooks, `cmd` |
| `command` | slash command handler (commands spec) | edge, bridge and session commands of v1 |
| `answer_sink` | questions whose answer goes somewhere besides the inbox | — |
| `text_check` | check or rewrite outgoing text; refuse with an exit code and a hint | private guard, logins, questions-as-text |
| `admission` | extra rule: is this session admitted, which bot does it post with | — |

## Requirements

### Requirement: Plugins are discovered, listed and switched per connection
The core SHALL load plugins from entry points and the local plugins directory; `aimost plugins` SHALL list them with kind,
version and source. A plugin SHALL be enabled per connection (`PLUGINS=<name>[,<name>]`); built-ins are enabled unless
listed in `PLUGINS_OFF`; optional built-ins (`health`, `admin`) ship with the core but are off until listed, like an installed
plugin. `AIMOST_PLUGINS=<name>[,<name>]` enables plugins for the whole process — the way a process without connections
(the edge) turns them on; for the global points (slash commands, read rules, `who` sources) it wins over `PLUGINS_OFF`. A plugin that fails to load MUST NOT stop the bridge: it is logged and skipped, and nothing it registered before failing
stays registered. Callbacks that receive a connection run only where the plugin is enabled; slash commands, read rules
and `who` sources are installed when the plugin is enabled on at least one connection.

#### Scenario: a broken plugin
- **WHEN** a plugin raises on import
- **THEN** the bridge starts, `aimost plugins` shows it as failed with the error type
Test: `tests/test_plugins.py` (Discovery), `tests/test_health.py` (Switch)

### Requirement: Plugins do not bypass authority
Plugins SHALL receive the sender role resolved by the core and MUST NOT be able to raise it. Events written by a `source`
SHALL carry the sender role the core assigns to that source (`peer` unless the connection lists the source in
`SOURCES_OWNER`).

#### Scenario: a source tries to write an owner event
- **WHEN** a source plugin writes an event with sender role `owner`, and the connection does not trust it
- **THEN** the event is stored as `peer`
Test: `tests/test_plugins.py` (SourceAuthority)

### Requirement: Answer sinks
An `answer_sink` plugin SHALL be able to post a question through the core's question mechanism with a sink key; a button
answer to such a question SHALL be passed to the sink only when pressed by the owner (`via = chat`), and the sink's result
SHALL be reported to the asking session as an `answer` event; a sink refusal SHALL be posted into the thread as
"⚠️ <sink> refused". Answers by co-owners, addressees, agents or unknown people MUST NOT reach a sink. The sink's questions
SHALL get their own section in "Waiting for you".

#### Scenario: a co-owner presses a sink question
- **WHEN** a co-owner presses a button of a sink question
- **THEN** the answer does not count, nothing is passed to the sink
Test: `tests/test_waiting.py` (Real.test_sink_gets_only_the_owners_answer, Real.test_sink_refusal_is_posted)

#### Scenario: the sink's own question id; questions an earlier version posted
- **WHEN** a role asks with `aimost post --ask … --sink <key> --ref <id>`, or the owner answers a question an earlier
  bridge version posted with the sink side's own id (no `ask-` id)
- **THEN** the sink gets the answer with `ref=<id>`; an old question goes to the sink whose `claims(id)` is true, and to
  none when no sink claims it
Test: `tests/test_waiting.py` (Real.test_sink_gets_its_own_question_id,
Real.test_a_question_an_earlier_version_posted_is_answered_through_the_sink_that_claims_it)

#### Scenario: the sink's own buttons; its side marks the answer done
- **WHEN** the sink has `buttons(spec, part)` (given `conn=` — the connection — when it takes it) and a role asks through it — the spec carries `ref` (the id on the sink's
  side); the sink returns Mattermost actions (each a `name` and an `integration.url`, e.g. a plugin-relative
  `/plugins/<id>/…` with its own `context`)
- **THEN** that item shows only the sink's buttons (ids `p<part>s<n>`, no edge buttons, no own-answer button), kept in the
  spec so the edge redraws them; broken actions refuse the question before it is posted
- **WHEN** the side that took the press merges into the post's props the mark `<prefix>_answers = {"<part>": {option | text,
  by, user_id, at, "done": "<who recorded it>"}}`
- **THEN** nothing is passed to the sink (whoever pressed — that side checked the rights), the asking session gets an
  `answer` event "… → <sink>: <answer>, recorded by <done>" once, the question counts as answered, and the bridge redraws
  the post (the answer instead of the buttons, the other props kept)
Test: `tests/test_waiting.py` (Real.test_sink_supplies_its_buttons_and_its_plugin_marks_the_answer_done,
Real.test_a_sink_with_broken_buttons_refuses_the_question), `tests/test_questions.py` (SinkButtons)

### Requirement: Text checks form a pipeline
Outgoing text SHALL pass the built-in checks and then enabled `text_check` plugins in a fixed order; the first refusal stops
the post with its exit code (plugins use codes 20–39) and hint. A check MAY rewrite text (e.g. expand a reference into a
link); a check that cannot reach its backend SHALL let the text through unchanged.

#### Scenario: a plugin refuses
- **WHEN** a text check plugin refuses a post with code 21
- **THEN** exit code 21 and the plugin's hint, nothing is posted
Test: `tests/test_plugins.py` (Switching, RefusalThroughPost)

### Requirement: Delivery is replaceable
Delivery to a session and waking SHALL go through the `delivery` interface; Claude Code hooks are the default
implementation, Codex hooks and the `cmd` wake (`<role> cmd <command>` in `WAKE_LIST`) are built in. A Channels (MCP)
adapter MAY be added later without changing inbox format or events.

#### Scenario: an agent woken by its own command
- **WHEN** a role in `WAKE_LIST` is `<role> cmd my-agent-wake`
- **THEN** the dispatcher runs `my-agent-wake` with the role, the event count and a prompt
Oracle: `chat-codex.test.sh`
