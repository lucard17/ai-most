# health Specification

## Purpose
`/health` answers one question in the chat: are the services of a product answering right now, per environment
(*contour*: `staging`, `prod`, …). It is an optional built-in plugin (`aimost_health`, kind `command`, an `edge` command):
the edge, which already runs next to the chat server, sends plain HTTP GETs to the services' own health endpoints and posts
a short table. It is not monitoring: no history, no alerts, no data freshness — only "does it answer as expected".

Upstream: the working version's `/health` in its edge (after the D8 freeze point). Ported as a plugin with the
product-specific parts (service names, known failure reasons, detail fields) moved from code into the targets file.

## Requirements

### Requirement: Off unless enabled, configured only by a file
The plugin SHALL be off unless listed: `AIMOST_PLUGINS=health` for a process without connections (the edge, and wherever
`aimost commands register` runs) or `PLUGINS=health` on a connection. Probe addresses SHALL come only from the targets file
`EDGE_HEALTH_TARGETS` (JSON, domain → contour → service → probe or `null`); command arguments only pick names from it.
`EDGE_HEALTH=off` SHALL answer "turned off" without reading the file or probing. Without a file the command says no
domains are configured.

#### Scenario: not listed
- **WHEN** `AIMOST_PLUGINS` does not name `health` and no connection lists it
- **THEN** `/health` is not in the registry, `aimost plugins` shows it as `built-in (optional)`, not enabled
Test: `test_health.Switch`

### Requirement: The targets file is checked whole; a bad version keeps the last good one
Names of domains, contours and services SHALL match `[a-z0-9][a-z0-9-]{0,31}`. A probe SHALL have `url` (scheme, host,
port — no path, credentials, query or fragment), an absolute `path` without `.`/`..` segments, a `rule` (`http`, `status`,
`role`), and optionally `redirect` (http only), `mode`, `reasons`, `details` (JSON rules only). Shown texts (`reasons`
values, detail labels) SHALL be plain words — no mentions, links, `|`, back-ticks, line breaks, at most 80 characters.
The file SHALL be re-read when it changes (mtime, inode, size). A version that fails any check SHALL be refused whole: the
reason (never the content) goes to the log, the last accepted version stays. A changed contour forgets its last poll.

#### Scenario: an address with a path slips in
- **WHEN** the file is replaced with one whose probe URL carries a path
- **THEN** the log says "targets refused (bad probe address)", the previous targets answer the next call
Test: `test_health.Config`, `test_health.Targets`, `test_health.Calls.test_a_changed_contour_forgets_its_last_poll`

### Requirement: Rules turn an answer into a status; nothing from the body reaches the chat unchecked
A probe SHALL give `ok`, `off`, `fail` (with a short reason made by the edge), `noprobe` (`null` in the file — normal) or
`unreachable`:
- `http`: 2xx, or the configured redirect (status and `Location` path prefix), never followed;
- `status`: 200 and JSON `status: "ok"`;
- `role`: 200, `ok: true` and `role` equal to the service name (another `role` — "the answer is not from this service");
  `ok: false` is read even with a 5xx;
- `mode`: the field SHALL be in `allowed` ("unknown mode") and equal to `expected` ("differs from the contour's setting");
  a matching `off` with `off_status` is `off`.
A failure reason reported by the service SHALL be shown only through the `reasons` map, otherwise "probe returned an
error". Details SHALL be shown only for fields listed in `details`: `count` — an integer ≥ 0 that is not a boolean,
`allowed` — a value from the list; anything else is dropped with a log line.

#### Scenario: a hostile body
- **WHEN** a service answers `{"ok": false, "role": "api", "reason": "@channel see http://…"}`
- **THEN** the row is 🔴 "probe returned an error"; nothing of the body is posted
Test: `test_health.Parse`

### Requirement: Probes are bounded
Probes of one contour SHALL run in parallel; each SHALL have one deadline (1 s, `EDGE_HEALTH_PROBE_S`) from connecting to
the last byte, so a body sent byte by byte is cut off; a body over 64 KiB is "answer too large". When no probe of a
contour got an HTTP answer, its rows SHALL be ❔ "no link to contour" instead of 🔴 for each service; when any answered,
the others' errors are their own failures.

#### Scenario: a contour behind a dead link
- **WHEN** every probe of `prod` fails to connect
- **THEN** the summary says "prod — no link to contour", the rows show ❔
Test: `test_health.Poll`, `test_health.Calls.test_unreachable_contour`

### Requirement: The edge does not wait for the network; at most one poll per contour per window
The command SHALL answer at once "⏳ checking…" and start the polls only after that answer is sent (no answer sent — no
poll). The result SHALL arrive as an ephemeral delayed answer to the call's `response_url`, of which only the
`/hooks/commands/<id>` path is used, sent to the edge's own Mattermost. A contour SHALL be polled at most once per
`EDGE_HEALTH_MIN_GAP_S` (10 s): a call during a poll joins it, a call inside the window takes the last result marked
"checked N s ago". A poll error (exception, deadline) is not a result: the next poll may start 2 s after it, until then a
call says "retry in N s". The whole call SHALL have a deadline (`EDGE_HEALTH_DEADLINE_S`, 5 s): a watcher sends
"❗ poll error: deadline", and a late result is dropped.

#### Scenario: two people call at once
- **WHEN** a second call arrives while the first one's poll of `staging` is running
- **THEN** both get the same result; the services were probed once
Test: `test_health.Calls`, `test_health.ThroughTheEdge`, `test_health.ClaimAfterDeadline`

### Requirement: Who may call and what is shown
Any member of the chat (owner, person, bot) MAY call; a guest (`system_guest`) or a caller without roles SHALL be refused.
`/health` SHALL show a summary per domain and only the rows that are not green; `/health <domain>` — services × contours
(`—` where a contour has no such service); `/health <domain> <service>` — that service per contour and its details table.
An unknown domain or service SHALL get a hint with the names from the file. Answers carry statuses, the edge's reasons and
whitelisted details — never addresses, ports or error texts.

#### Scenario: a guest
- **WHEN** a guest calls `/health`
- **THEN** "Open to members of the chat."; nothing is probed
Test: `test_health.Calls.test_refusals_and_help`, `test_health.Calls.test_summary_domain_and_service_views`

### Requirement: Audit
Every call SHALL leave one `health audit` log line (time, user id, arguments, outcome — `completed`, `timeout`, `denied`,
`disabled`, `invalid_arguments`, `unconfigured`, `invalid_response_url`, `error` — and counts per domain); every contour
poll — one line (domain, contour, answered, connection errors, duration, error class). No addresses, reasons or rows.

#### Scenario: an audit line
- **WHEN** a call completes
- **THEN** its line holds the counts per status and no address of any probe
Test: `test_health.Calls.test_audit_has_no_addresses_or_reasons`; through the real edge — `test_edge_it` (NEW)
