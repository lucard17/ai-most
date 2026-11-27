"""``aimost post`` — post as the role's bot (specs: text, questions).

  aimost post [--via <conn>] [--channel <name>|dm|dm:<login>] [--thread <root_id>|--new-thread] [--file <path>]…
             [--attach] [--invite] [--plain] ["<text>"]
  aimost post [--via …] [--channel …|--thread …] --ask "<question>" "<option>"… [--rec N]
  aimost post [--via …] [--channel …|--thread …] --ask <spec>.json
      [--to <login>[,<login>…]] [--ttl <hours>] [--blocking] — flags may come before or after --ask
      [--sink <key> [--ref <id>]] — the owner's answer goes to a plugin's answer sink (``aimost plugins``), with the
      id of the question on its side
  aimost post [--via …] --channel <name> --pin <key> "<text>"|--file <doc>.md
  aimost post [--via …] --edit <post_id> "<new text>"
  aimost post [--via …] --cancel <ask-id|post_id> "<why>"
  aimost post [--via …] --conclude <DM thread root> "<outcome>"
  aimost post --open-asks

Text: a ``.md`` file without text is sent as the post's text (long — in parts into the thread), as an attachment only
with ``--attach``. Two or more questions as text are refused (code 4) — ask with buttons; a quote or a log — ``--plain``.
A wall of text is refused (code 11): the first line over 200 characters, a paragraph or a line over 350, or over 500 without
a field, a list or a heading — the point first, fields in bold each on its own line, lists as lists (``--plain``, a ``.md``
document and a pin are not checked).
A question in a channel needs ``--to`` (code 2); buttons are accepted only from the addressees, who must be people.
Unknown logins — code 6, mentioned people not in the channel — code 7 (``--invite`` adds them), the bot not in the
channel — code 8. Not admitted, no bot, someone else's post — code 3.
Role: ``AIMOST_ROLE`` or the Claude Code session's name. Connection: ``--via`` from the inbox event, else the role's own.
"""

from __future__ import annotations

import sys
import urllib.error
from typing import List

from . import plugins, post, questions

VALUED = ("--via", "--channel", "--thread", "--file", "--edit", "--to", "--pin", "--cancel", "--ttl", "--conclude", "--sink",
          "--ref")
FLAGS = ("--plain", "--new-thread", "--attach", "--invite", "--open-asks", "--blocking")


def parse(argv: List[str]):
    """Flags anywhere: before the text, and after ``--ask`` (its tail are options and ``--rec``)."""
    opts: dict = {}
    files: List[str] = []
    flags = set()

    def take(a: List[str]):
        if a[0] in VALUED and len(a) >= 2:
            if a[0] == "--file":
                files.append(a[1])
            else:
                opts[a[0][2:]] = a[1]
            return a[2:]
        if a[0] in FLAGS:
            flags.add(a[0][2:])
            return a[1:]
        return None

    a = list(argv)
    while a[:1]:
        rest = take(a)
        if rest is None:
            break
        a = rest
    if a[:1] == ["--ask"]:
        tail, i = [a[0]], 1
        while i < len(a):
            rest = take(a[i:])
            if rest is None:
                tail.append(a[i])
                i += 1
            else:
                i = len(a) - len(rest)
        a = tail
    return opts, files, flags, a


def main(argv: List[str]) -> int:
    if any(x in ("-h", "--help") for x in argv):
        print(__doc__)
        return 0
    opts, files, flags, a = parse(argv)

    def die(msg: str, code: int = 2) -> int:
        print("aimost post: " + msg, file=sys.stderr)
        return code

    try:
        is_ask = a[:1] == ["--ask"]
        spec = questions.from_args(a[1:]) if is_ask else None
        if opts.get("edit") and (is_ask or files or len(a) != 1):
            return die('--edit <post_id> "<new text>" — text only: buttons and files are not edited')
        if "cancel" in opts and (len(a) != 1 or files or is_ask):
            return die('--cancel <ask-id|post_id> "<why>" — the reason is required, no buttons or files')
        if "conclude" in opts and (len(a) != 1 or files or is_ask):
            return die('--conclude <DM thread root> "<outcome>" — the outcome in one line')
        if "open-asks" in flags and (a or files):
            return die("--open-asks — no text or files")
        if "blocking" in flags and not is_ask:
            return die("--blocking — only with --ask: a question without whose answer the work stands")
        if ("sink" in opts or "ref" in opts) and not is_ask:
            return die("--sink / --ref — only with --ask")
        if "ref" in opts and "sink" not in opts:
            return die("--ref <id> — only with --sink <key>: the id belongs to the sink's side")
        ttl = None
        if "ttl" in opts:
            if not is_ask:
                return die("--ttl <hours> — only with --ask")
            try:
                ttl = float(opts["ttl"])
            except ValueError:
                ttl = -1.0
            if not ttl > 0:
                return die(f"--ttl {opts['ttl']!r}: hours, a number above zero")
        if not is_ask and len(a) > 1:
            return die("one text argument (quote it); see --help")
        role = post.role_name()
        if not role:
            return die("no role name (not in a Claude Code session and no AIMOST_ROLE)")
        if "open-asks" in flags:
            out = post.open_asks(role)
        else:
            via = opts.get("via")
            if not via and opts.get("cancel", "").startswith("ask-"):     # cancel where it was asked
                from . import askstore
                via = (askstore.asks(role).get(opts["cancel"]) or {}).get("via")
            p = post.Poster(role, via)
            where = dict(channel=opts.get("channel", "dm"), thread=opts.get("thread", ""),
                         new_thread="new-thread" in flags)
            if "cancel" in opts:
                out = p.cancel(opts["cancel"], a[0])
            elif "conclude" in opts:
                out = p.conclude(opts["conclude"], a[0])
            elif opts.get("edit"):
                out = p.edit(opts["edit"], a[0], plain="plain" in flags)
            elif is_ask:
                if opts.get("sink") and plugins.sink(p.conn, opts["sink"]) is None:
                    return die(f"--sink {opts['sink']!r}: no such answer sink on connection {p.conn['NAME']} "
                               "(`aimost plugins`)")
                out = p.ask(spec, to=opts.get("to", ""), invite="invite" in flags, ttl=ttl,
                            blocking="blocking" in flags, sink=opts.get("sink", ""), ref=opts.get("ref", ""), **where)
            else:
                out = p.send(a[0] if a else "", files=files, attach="attach" in flags, invite="invite" in flags,
                             plain="plain" in flags, pin=opts.get("pin", ""), **where)
    except (post.PostError, questions.AskError) as e:
        return die(str(e), e.code)
    except urllib.error.HTTPError as e:
        return die(f"chat server: HTTP {e.code} {e.reason}", 1)
    except urllib.error.URLError as e:
        return die(f"chat server unreachable: {e.reason}", 1)
    for line in out:
        print(line)
    return 0
