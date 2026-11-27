# questions Specification

## Purpose
Sessions ask people with buttons in chat (`lmost post --ask`), not with text: a question has addressees, the answer comes
back as an event into the asking session's inbox, a free-text answer is checked by retelling before acting. Questions of
answer-sink plugins (see extensions) use the same mechanism, but their answer goes to the sink.

## Requirements

### Requirement: A question has addressees
A `--ask` question SHALL have addressees: in a direct message by default — the owner (or the person of that DM); in a
channel `--to` MUST be given (otherwise exit code 2, no post). Buttons MUST be accepted only from addressees: the edge
refuses an outsider's press with a hint, the bridge does not count it, an outsider's answer MUST NOT close a question to the
owner.

#### Scenario: a non-addressee presses
- **WHEN** a question "ready to merge?" to an MR author with `--to`, and an outsider presses a button
- **THEN** hint "your opinion — in the thread", the answer does not count
Test: `tests/test_questions.py` (Render — addressees in the button context); the edge's refusal and the bridge's count — Oracle: `chat-ask-to.test.sh`

#### Scenario: a channel question without addressees
- **WHEN** `--ask` in a channel without `--to`
- **THEN** exit code 2, no post
Test: `tests/test_post.py` (Asks.test_channel_ask_needs_to_and_flags_after_ask)

### Requirement: Addressees are notified
Addressees SHALL be mentioned in the first line, with the question text in the post body, not only in the attachment card;
a question whose addressee would get no notification MUST NOT be sent. Flags SHALL be read both before and after `--ask`;
the recommendation `rec` is an option number or its text.

#### Scenario: a question to two people
- **WHEN** `--ask` with two items and `--to` two people
- **THEN** the first line mentions both, item titles are in the post body
Test: `tests/test_post.py` (Args, Asks.test_channel_ask_needs_to_and_flags_after_ask), `tests/test_questions.py` (Render.test_mentions_first_line_and_titles_in_body, Spec)

### Requirement: A free-text answer — retell before acting
An answer through "✍️ own answer" SHALL arrive as an event that requires, before acting, retelling it in the thread by its
consequences and checking with buttons "right / not so"; a button answer MUST arrive without that requirement.

Acting on one's own reading of a free-text answer without the check MUST NOT happen.

#### Scenario: the owner answers in words
- **WHEN** the owner answered a question with own text
- **THEN** the `answer` event carries the retelling requirement
Test: `tests/test_waiting.py` (Real.test_free_text_answer_carries_the_retell_demand, Real.test_button_answer_reaches_the_session_once)

### Requirement: Questions as buttons, not text
A post with two or more questions as text MUST NOT be sent (exit code 4, hint `--ask`); text that is not a question to
people (a quote, a log) SHALL pass with `--plain`. The same rule SHALL apply to edits.

#### Scenario: two questions as text
- **WHEN** the post "Deploy? Or wait?"
- **THEN** exit code 4, nothing is posted
Test: `tests/test_post.py` (Basics.test_two_questions_as_text, Channels.test_edit_own_only)

### Requirement: A stale question is cancelled
A session SHALL cancel its question with `--cancel <ask-id|post_id> "<why>"`: the post is edited in place, buttons removed,
it starts with "⏹ Question cancelled: …"; someone else's question MUST give exit code 3. An answer to a cancelled question
MUST NOT reach the inbox. `--ttl <hours>` SHALL cancel by deadline; no deadline by default. `--open-asks` SHALL list the
session's open questions.

#### Scenario: the question lost its point
- **WHEN** the session cancels its question with `--cancel` and a reason
- **THEN** no buttons; a press on stale markup does not reach the inbox
Test: `tests/test_post.py` (Asks.test_cancel_open_asks, Asks.test_answered_or_deleted_leaves_the_list), `tests/test_questions.py` (Misc.test_cancelled_patch_keeps_props); an answer after cancel and the ttl sweep — Test: `tests/test_waiting.py` (Real.test_answer_after_cancel_does_not_count, Real.test_ttl_cancels_at_the_deadline)

### Requirement: Questions to the owner are pinned in "Waiting for you"
Every `--ask` SHALL be its own post with its own ask-id; the number of open questions per session MUST NOT be limited. With
`WAITING_POST` on, the bridge SHALL keep one pinned post "Waiting for you" in the owner's direct message: open questions to
the owner from all sessions and from answer sinks, `--blocking` on top with 🔴, then by age; answered and cancelled ones
MUST NOT stay in the list.

#### Scenario: two questions of one session
- **WHEN** a session asked the owner two `--ask` questions in a row
- **THEN** two posts with different ask-ids, both are lines in "Waiting for you"
Test: `tests/test_waiting.py` (Real.test_pin_drop_and_snooze, Real.test_rows_blocking_on_top_then_oldest)

### Requirement: The bridge reminds about what waits for the owner
With `WAITING_POST` on, the bridge SHALL remind in the owner's direct message: a 🔴 question waiting longer than
`WAITING_REMIND_AFTER_H` (1 h by default) — one post with a link, repeated not more often than `WAITING_REMIND_REPEAT_H`
(2 h); at `WAITING_DIGEST_AT` hours (10:00, 18:00) — a digest "N waiting, M of them 🔴" and the three oldest. Nothing
waiting — no digest.

#### Scenario: a 🔴 question hangs
- **WHEN** a 🔴 question waits since 13:00
- **THEN** at 14:05 — a reminder, at 15:00 — none, at 16:10 — the second; an answered one — no reminders
Test: `tests/test_waiting.py` (Real.test_blocking_question_reminders, Real.test_digest_once_per_slot_and_none_when_empty)

### Requirement: A resolved question shows the answer
An answered item SHALL show "question → answer — who" in the post; a fully answered question SHALL show it for every item.
An empty "Resolved" post MUST NOT remain.

#### Scenario: the only item answered
- **WHEN** an addressee presses an option of the only item
- **THEN** the post starts with ✅ and contains "question → **answer** — @who"
Test: `tests/test_questions.py` (Render.test_partial_and_done); the press itself — Test: `tests/test_waiting.py` (Real.test_button_answer_reaches_the_session_once), `tests/test_edge_it.py`

### Requirement: Reminder to cancel hanging questions
If during a turn the owner spoke not by a button (terminal, chat message, free-text answer), and the session has open
questions to the owner, the Stop hook SHALL once per turn block stopping: the list of open questions and the demand to
cancel each with `lmost post --cancel` or keep it. Without the owner's words or without open questions the hook MUST NOT
interfere.

#### Scenario: the owner clarified the task in the terminal
- **WHEN** the session has an open question to the owner, and the owner wrote in the terminal during this turn
- **THEN** at Stop — the list and the demand; the second Stop of the same turn — silent
Oracle: `chat-stale-asks.test.sh`

#### Scenario: a turn without the owner's words
- **WHEN** the turn was started by another session's message, an open question exists
- **THEN** the Stop hook is silent about questions
Oracle: `chat-stale-asks.test.sh`

### Requirement: The owner drops a question from "Waiting for you"
Every `--ask` line in "Waiting for you" SHALL have a "not relevant" button. The owner's press SHALL cancel the question with
reason "dropped by @<owner> from Waiting for you" and put a `cancelled` event into the asking session's inbox; a press by
anyone else MUST NOT cancel it.

#### Scenario: the owner drops a session's question
- **WHEN** the owner presses "not relevant" on a session's question line
- **THEN** the question is cancelled in place, the line is gone, the session's inbox gets `cancelled`
Test: `tests/test_waiting.py` (Real.test_pin_drop_and_snooze, Real.test_pin_marks_only_on_the_pin)

### Requirement: Discuss a question in a direct message
A `--ask` question outside a direct message SHALL have one button "💬 discuss in DM". An addressee's press SHALL open a new
thread "Discussion: <first line of the question> — <link to the original post>" in their DM with the session's bot, post
one line "discussion moved to DM with @<who>" into the original thread, and put a `discuss` event with the DM thread into
the session's inbox.

#### Scenario: an addressee opens a discussion
- **WHEN** an addressee of a channel question presses "💬 discuss in DM"
- **THEN** a "Discussion: …" post in their DM with the bot, a line in the original thread, a `discuss` event in the inbox
Test: `tests/test_waiting.py` (Real.test_discussion_in_a_dm)

### Requirement: A discussion does not break the question
Answer buttons MUST stay live after a discussion is opened. A non-addressee's press MUST NOT open a discussion; a repeated
press by the same person MUST NOT open a second thread.

#### Scenario: a non-addressee presses
- **WHEN** someone who is not an addressee presses the discuss button
- **THEN** a hint, no DM post, no event
Test: `tests/test_waiting.py` (Real.test_discussion_in_a_dm); the hint — the edge

### Requirement: The outcome of a discussion — one line in the original thread
A session SHALL send the outcome with `lmost post --conclude <DM thread root> "<outcome>"`: one line "Outcome of the
discussion with @<person>: <outcome>" in the original thread, a mark in the DM thread. Concluding someone else's discussion
MUST give exit code 3; text checks SHALL apply as to a normal post.

#### Scenario: the session concludes
- **WHEN** the session calls `--conclude` with the root of its DM thread
- **THEN** the outcome line in the original thread, a mark in the DM thread
Test: `tests/test_post.py` (Conclude)

### Requirement: "Waiting for you" in sections
"Waiting for you" SHALL split lines into a section "Chat questions" and one section per answer sink. A button mark MUST NOT
be lost when the pin is redrawn.

#### Scenario: chat questions and a sink's question
- **WHEN** two session questions and one sink question wait for the owner
- **THEN** "Chat questions — 2" and "<sink> — 1", each line in its section
Test: `tests/test_waiting.py` (Offline.test_pin_sections_and_unique_action_ids, Real.test_redraw_does_not_lose_a_mark)

### Requirement: Snoozed is out of the list
Every "Waiting for you" line SHALL have a "snooze…" menu: a day, a week, until I say; only the owner snoozes. A snoozed
question MUST NOT show in the list, reminders, digest and the Stop hook until the deadline or "bring back…"; the bottom of
the pin SHALL show "💤 snoozed: N". The asking session SHALL get a `snoozed` event.

#### Scenario: the owner snoozes a question for a day
- **WHEN** the owner picks "snooze… a day" on a session's question line
- **THEN** the line is gone, "💤 snoozed: 1" at the bottom, the session gets `snoozed`, the Stop hook is silent about it;
  a day later the line is back
Test: `tests/test_waiting.py` (Real.test_pin_drop_and_snooze)

#### Scenario: a sink question snoozed "until I say"
- **WHEN** the owner snoozes a sink question "until I say"
- **THEN** the line is gone even a month later; "bring back…" returns it to its section
Test: `tests/test_waiting.py` (Real.test_snooze_by_someone_else_and_back)

### Requirement: Questions asked past the registry get into the registry
On `serve` start and on `lmost link backfill-asks [--days N]` the bridge SHALL record into the question registry open
button questions of the connection's bots within the window (14 days by default) that are missing from it. Answered,
cancelled, deleted questions, posts by non-bots and questions already recorded MUST NOT be recorded again. A question older
than 7 days (`BACKFILL_SNOOZE_DAYS`) SHALL become "snoozed until I say" right away.

#### Scenario: a question asked before the registry existed
- **WHEN** an unanswered bot question hangs in the owner's DM, and it is not in the registry
- **THEN** after `backfill-asks` it is in the registry of the session from `props.agent`, addressed to the owner, and in
  "Waiting for you"; a repeated run adds nothing
Test: `tests/test_waiting.py` (Real.test_backfill)
