# Ask where the owner spoke last (`auto` mode)

## Why
A session with its own bot always asks the owner in chat: the ask guard refuses the terminal dialog (AskUserQuestion). When the
owner sits at that session's terminal and types to it, the question still goes to Mattermost — the owner must switch to chat to
answer a question about what they just typed.

Today `auto` is only "no label": the mode is derived (`chat` with a bot or in `WAKE_LIST`, else `cli`), and a derived `chat`
behaves like an explicit one.

## What changes
- `auto` (no label) becomes a mode of its own: the inbox wakes and delivers as in `chat`, and a blocking question goes **where
  the owner spoke last** — the terminal (AskUserQuestion allowed) or the chat thread of the owner's last delivered event (refused
  with a hint naming that thread).
- "Spoke" = the owner's input typed into the session (a turn start or a mid-turn queued input, not an inbox wake-up), or an owner's
  chat event handed to the session (a message, an answer by button, a reaction). The latest of the two wins.
- Explicit labels keep their meaning: `chat` — questions always in chat; `cli` — always in the terminal, no waking.
- `aimost inbox mode <role>` says `auto` in the brackets (`chat (auto: own bot; questions where the owner spoke last …)`);
  the first word stays the delivery mode.
- New chat command `/agent mode <role> [cli|chat|auto]` (owner only, the answer in the command post's thread) — the same setting from Mattermost
  (l17 28.11).
- Unchanged: an unlabelled session without a bot and not in `WAKE_LIST` stays `cli` (not woken, questions in the terminal).

## Impact
- Specs: delivery ("Session mode"), authority ("A session with a bot asks the owner in chat"), commands (new `/agent mode`).
- Code: `inbox.mode_of` (report `auto`), `inbox.write_turn_marks` (owner mark with via and thread), `hooks.ask_guard`,
  `turn_origin` (last typed input time), `cli_inbox mode`.
- Behaviour change for every unlabelled role with a bot: a question after the owner typed in the terminal is asked there.
- ai-most-agents `/session-config mode auto` already maps to "no label" — no change there.
