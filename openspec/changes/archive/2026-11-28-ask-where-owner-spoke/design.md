# Design

## Where the owner spoke last
Two times are compared, both already observable:

- **terminal** `T` — the owner's typed input **in the current turn only** (l17 28.11: «терминал считается только в своём ходе»):
  the turn start if `turn_origin.classify` says `human`, else the newest mid-turn `queued_command` attachment with
  `origin.kind=human` after the turn start (`owner_typed_since` already reads these). A turn started by an `/inbox` wake-up
  (`wake`) or by a session's message (`peer`) has `T` = 0 unless the owner typed into it. New helper
  `turn_origin.typed_in_turn(path) -> int` (0 — none).
- **chat** `C` — written when the inbox hands the session an event of the owner (`role=owner`, with `via`) of kind `message`,
  `answer` or `reaction`: new mark `<marks>/<role>.owner-at` = `"<epoch> <via> <thread>"` (thread may be empty — a DM). The
  existing `.chat` mark stays as it is (the confirm guard reads it; it counts only messages).

`T > C` → the terminal; `C ≥ T` → chat, in the mark's thread; neither → chat (today's behaviour for a role with a bot: a background
session has no terminal input at all).

Why not only "who started the current turn": a turn the owner started in the terminal may take an answer from a thread mid-turn
(workbench's second example — "an answer came from the thread → the question goes to that thread"); within the turn the latest word
wins. Why not the owner's typing in an earlier turn: a later turn started by a wake-up or a peer means the owner may have left the
terminal (l17's answer to example 4).

Why times and not a flag flipped on input: both sources exist without a new writer in the hot path; the transcript is read by its
tail only (`TAIL_BYTES`), as the delivery hook already does.

## Modes
| Label | Wakes / mid-turn delivery | Blocking question |
| --- | --- | --- |
| `cli` | no | terminal |
| `chat` | yes | chat, always |
| none = `auto` | as derived today (`chat` with a bot or in `WAKE_LIST`, else `cli`) | where the owner spoke last; a derived `cli` — terminal |

`mode_of` keeps returning the delivery mode as its first value (callers in dispatch, link, hooks use it for delivery); the source
string becomes `auto: own bot` / `auto: wake list` / `auto: default`, and a new `inbox.ask_place(role, transcript) -> ("terminal",
"") | ("chat", "<via> <thread>")` answers the question placement.

## Refusal text
In chat placement the guard names the thread: `aimost post --via <via> --thread <thread> --to <owner> --ask …` (no thread — the
DM form), and adds one line: "You typed in the terminal last at HH:MM, the chat at HH:MM — the question goes to chat; to keep
questions in the terminal: `/session-config mode cli`."

## Risks
- A transcript we cannot read (Codex, no path): `T` = 0 → chat, as today.
- Clock: both times are this machine's clock (mark written by the hook, transcript timestamps from Claude Code) — same host.
- A pasted automatic prompt into the pane counts as typed (`human`), as it does for the peer guard today.
