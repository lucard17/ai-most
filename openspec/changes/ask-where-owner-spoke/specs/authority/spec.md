## MODIFIED Requirements

### Requirement: A session with a bot asks the owner in chat
A session that has a bot SHALL ask the owner where the owner spoke last. With an explicit `chat` label — always with buttons in
chat (`aimost post --ask`); with an explicit `cli` label — in the terminal. Without a label (`auto`): the terminal dialog
(AskUserQuestion) SHALL be allowed when the owner typed into the session in the current turn (started it by typing, or typed into
it mid-turn) and no owner's chat event was handed to the session after that; otherwise it SHALL be refused with a hint to `--ask`
in the thread of the owner's latest chat event (the DM when there is none). An inbox wake-up, a session's message and the owner's
typing in an earlier turn MUST NOT count as the owner typing. Sessions without a bot and subagents are not affected.

#### Scenario: a question in the terminal, label chat
- **WHEN** a session with a bot and the label `chat` calls the terminal question dialog
- **THEN** refused with a hint to `--ask`
Test: `tests/test_hooks.py` (AskGuard)

#### Scenario: auto, the owner typed last
- **WHEN** an unlabelled session with a bot got the owner's chat message at 10:00, the owner typed "do X" into its terminal at
  10:05, and the session calls the terminal question dialog
- **THEN** the dialog is allowed
NEW. Test: `tests/test_hooks.py` (AskGuardAuto)

#### Scenario: auto, an answer came from a thread later
- **WHEN** the owner typed at 10:05, then the owner's button answer from thread `t1` was handed to the session at 10:10, and the
  session calls the terminal question dialog
- **THEN** refused; the hint names `--thread t1`
NEW. Test: `tests/test_hooks.py` (AskGuardAuto)

#### Scenario: auto, woken from chat
- **WHEN** an unlabelled session with a bot was woken by `/inbox` and given the owner's message from thread `t2`, and nobody typed
  in its terminal
- **THEN** a terminal question is refused with `--thread t2`
NEW. Test: `tests/test_hooks.py` (AskGuardAuto)

#### Scenario: auto, a turn started by another session
- **WHEN** the owner typed into the session at 10:05 (that turn ended), at 10:20 another session's message started a new turn, and
  the session calls the terminal question dialog
- **THEN** refused with a hint to `--ask` (the owner's latest chat thread, or the DM)
NEW. Test: `tests/test_hooks.py` (AskGuardAuto)
