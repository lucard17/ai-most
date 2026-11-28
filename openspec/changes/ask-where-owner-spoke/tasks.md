# Tasks

## 1. Where the owner spoke last
- [ ] `turn_origin.typed_in_turn(path)`: the `human` turn start, else the newest `queued_command` (`origin.kind=human`) after the turn start; 0 for a `wake`/`peer` turn without typing
- [ ] `inbox.write_turn_marks`: `<role>.owner-at` = `"<epoch> <via> <thread>"` for owner `message|answer|reaction`
- [ ] `inbox.ask_place(role, transcript)`
Check: `python3 -m unittest tests.test_turn_origin tests.test_inbox` (new cases fail before the code)

## 2. Ask guard and mode report
- [ ] `hooks.ask_guard`: explicit `chat` — refuse as today; `cli` — pass; no label — `ask_place`; the hint names the thread and the two times
- [ ] `mode_of` source `auto: …`; `aimost inbox mode <role>` prints delivery and question place
- [ ] spec scenarios above as tests (AskGuardAuto, SessionMode)
Check: `python3 -m unittest tests.test_hooks tests.test_inbox`; full suite green

## 3. Live check
- [ ] l17's machine: an unlabelled role with a bot — type in its terminal, it asks → terminal dialog; answer a button in its thread, it asks → chat in that thread
Check: two transcripts / posts named in the PR
