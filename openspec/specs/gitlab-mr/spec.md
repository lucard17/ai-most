# gitlab-mr Specification

## Purpose
Built-in `source` plugin. A session opened a merge request and moved on; while it is busy or asleep, people approve,
comment, merge or close the MR, a pipeline fails. GitLab notifies people, not sessions. The plugin links an MR to the
session that created it and turns MR activity into events in that session's inbox. It also ships a `text_check` that turns
`repo!N` references into links with titles.

## Requirements

### Requirement: An MR is linked to the session that created it
After a successful MR-creating command the hook SHALL link the MR to the session (a subagent links it to its parent
session). MR-creating commands are `glab mr create` plus command words from `MR_CREATE_PATTERNS` (connection key or
`AIMOST_MR_CREATE_PATTERNS`); success is an MR URL in the
output, not the exit code. Network work runs in the background; the hook prints nothing and never blocks.

#### Scenario: a wrapper script creates the MR
- **WHEN** `MR_CREATE_PATTERNS` contains `my-mr`, and the session runs `my-mr` which prints an MR URL
- **THEN** the MR is linked to the session
Test: `tests/test_gitlab.py`

### Requirement: MR activity becomes inbox events
For a linked MR, approval and its removal, notes by people (not the session's own account), a failed pipeline on HEAD (once
per sha), merged and closed SHALL arrive as events in the session's inbox. A green pipeline and anything that happened
before linking MUST NOT arrive. Merged or closed SHALL unlink the MR.

#### Scenario: the MR is merged
- **WHEN** an MR opened by the session is merged
- **THEN** the session's inbox gets 🏁, the MR is unlinked
Test: `tests/test_gitlab.py`

### Requirement: MR references become links
The text check SHALL turn `repo!N` and `repo !N` into a link with the MR title from GitLab (project
`<GITLAB_GROUP>/repo`) (not duplicated if already
there), MUST NOT touch code spans, and a bare `!N` MUST give exit code 20 with a hint. GitLab unreachable — the text goes
as is.

#### Scenario: an MR in a report
- **WHEN** the post "merged backend!12 to prod"
- **THEN** the text has a link and the MR title
Test: `tests/test_gitlab.py` (Links)

### Requirement: Optional
Without `glab` (or with `glab` never logged in — no user config; `AIMOST_GLAB_BIN` set explicitly counts as present) or
with the plugin switched off, the bridge SHALL work as without it: no hook output, no timer errors, no text check.

#### Scenario: no glab
- **WHEN** `glab` is not installed
- **THEN** `aimost setup` does not install the MR timer, hooks stay silent
Test: `tests/test_gitlab.py` (Hook, Available, `once` without glab); the timer part — with `aimost setup`
