## ADDED Requirements

### Requirement: The owner sets a session's mode from chat
A `bridge` command `/agent mode <role> [cli|chat|auto]` SHALL show the role's mode (delivery and where a question goes now) and,
with a value, set it the same way as `aimost inbox mode <role> <value>` (`auto` removes the label). Only the owner of the role MAY
set it (the core's check, as for every bridge command); the answer SHALL come in the command post's thread. A background session MUST NOT be set to `cli` (nobody reads its
terminal) — refused with the reason, as `/session-config` does.

#### Scenario: away from the laptop
- **WHEN** the owner calls `/agent mode workbench chat` from the phone
- **THEN** the label `chat` is set; the answer reads `workbench: chat (label …) — questions in chat`; the next terminal
  question of workbench is refused with a hint to `--ask`
NEW. Test: `tests/test_commands.py` (Mode)

#### Scenario: not the owner
- **WHEN** another person calls `/agent mode workbench cli`
- **THEN** refused ("is not open to you"); the mode is unchanged
Test: `tests/test_link.py` (test_command_closed_to_the_caller)

#### Scenario: a background session is not set to cli
- **WHEN** the owner calls `/agent mode workbench cli`, and workbench runs as a background session
- **THEN** refused with the reason; the mode is unchanged
NEW. Test: `tests/test_commands.py` (Mode)
