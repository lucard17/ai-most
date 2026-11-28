## MODIFIED Requirements

### Requirement: Session mode — chat or cli
A session's mode SHALL be `chat` (the inbox wakes it and delivers mid-turn, questions go to chat), `cli` (cut off from the inbox:
never woken, events wait for `/inbox` typed by the owner, questions in the terminal), or `auto` — no label. In `auto` delivery
SHALL follow the derived mode (`chat` if the role is in `WAKE_LIST` or has its own bot, otherwise `cli`), and a blocking question
SHALL go where the owner spoke last (authority spec). An explicit label (`aimost inbox mode <role> chat|cli`) SHALL win;
`aimost inbox mode <role> auto` SHALL remove it. A role, background or Codex session SHALL label itself `chat` on its first event
if unlabelled (`--if-unset`); a launch MUST NOT override an explicit label. In `cli` mode the dispatcher MUST NOT wake the session,
the delivery hook MUST NOT deliver on PostToolUse nor block Stop and SHALL show the owner "📬 N new in inbox — /inbox";
`aimost inbox put` SHALL store the event and tell the sender the session answers on `/inbox`; the bridge SHALL react ⌨️ instead
of 📬. The peer guard MUST act in `cli` as in `chat`.

#### Scenario: a session started by hand
- **WHEN** the owner starts `claude`, names the session `foo`, it has no bot and is not in `WAKE_LIST`
- **THEN** `aimost inbox mode foo` prints `cli (auto: default)`; no wake-ups
Test: `tests/test_inbox.py` (SessionMode)

#### Scenario: an unlabelled role with a bot
- **WHEN** `foo` has its own bot and no label
- **THEN** `aimost inbox mode foo` prints `chat (auto: own bot; questions where the owner spoke last …)` — the first word stays
  the delivery mode, callers read it
NEW. Test: `tests/test_inbox.py` (SessionMode)

#### Scenario: another session writes to a cli session
- **WHEN** a session calls `aimost inbox put foo --from hash "…"`, and `foo` is in `cli`
- **THEN** the event is stored, the sender sees "foo is in CLI mode — it answers when its owner runs /inbox", exit 0
Test: `tests/test_inbox.py` (SessionMode)
