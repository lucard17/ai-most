"""``aimost confirm --spec <action.json>`` — ask the owner to confirm a dangerous action with a button (spec: authority).

The action is described by fields (``aimost.confirm_spec``):

    {"do": "what I do — one sentence", "why": "why, and at whose request", "steps": ["what happens — a step", …],
     "keeps": "what does not change", "rollback": "how to undo it | irreversible: …",
     "mrs": ["repo!N", …], "merges": ["repo!N", …], "commands": ["command", …]}

do, why, steps, keeps, rollback are required; ``merges`` — the merge requests the action merges (only their merge is
covered by the "yes"); ``mrs`` — the ones it is about (a card each in the post); ``commands`` — kept with the
confirmation, not posted. A bare command in a meaning field, a missing field, an already merged or closed merge request
in ``merges`` — code 4, nothing is posted. ``--spec -`` reads the description from stdin.

The first form, two free strings ``"<what I do>" "<scope>"``, is refused (code 2): such posts did not tell the owner what
would happen.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import List

from . import confirm, confirm_spec, inbox, mattermost

SPEC_HINT = ("describe the action by fields: aimost confirm … --spec <action.json> with do, why, steps, keeps, rollback "
             "(+ mrs, merges, commands) — `aimost confirm --help`")


def main(argv: List[str]) -> int:
    p = argparse.ArgumentParser(
        prog="aimost confirm", formatter_class=argparse.RawDescriptionHelpFormatter, description=__doc__,
        epilog="Exit 0 — confirmed, prints the id (then run AIMOST_CONFIRM=<id> <command>); 1 — declined; 2 — expired, "
               "the first form, or --to outside the allowed list (no post); 3 — the session may not post; 4 — the "
               "description is not in form (no post), or a chat server error.")
    p.add_argument("legacy", nargs="*", help=argparse.SUPPRESS)
    p.add_argument("--spec", default="", help="the action described by fields: a JSON file, or - for stdin")
    p.add_argument("--via", default="", help="connection (default: the session's own)")
    p.add_argument("--channel", default="", help="channel name, or dm (default)")
    p.add_argument("--thread", default="", help="thread root post id")
    p.add_argument("--to", default="", help="logins to mention, comma-separated (only allowed confirmers)")
    p.add_argument("--timeout", type=float, default=confirm.TTL_S, help="seconds to wait (default 900)")
    a = p.parse_args(argv)

    def fail(msg: str, code: int) -> int:
        print(f"aimost confirm: {msg}", file=sys.stderr)
        return code

    if a.legacy:
        return fail(("--spec takes no positional text: everything goes into the description" if a.spec else
                     "the two-string form «what I do» «scope» is no longer accepted — " + SPEC_HINT), 2)
    if not a.spec:
        return fail(SPEC_HINT, 2)
    try:
        raw = json.load(sys.stdin if a.spec == "-" else open(a.spec, encoding="utf-8"))
    except (OSError, ValueError) as e:
        return fail(f"--spec {a.spec}: {e}", 4)
    spec, errs = confirm_spec.check(raw)
    if errs:
        return fail("the description is not in form — nothing posted:\n  " + "\n  ".join(errs) + "\n  " + SPEC_HINT, 4)
    role = inbox.session_name()
    if not role:
        return fail("no session name (set AIMOST_ROLE or /rename the session)", 2)
    try:
        cid = confirm.request(role, spec, via=a.via, channel=a.channel, thread=a.thread,
                              to=a.to.split(",") if a.to else (), timeout=a.timeout,
                              log=lambda m: print(f"aimost confirm: {m}", file=sys.stderr))
    except confirm.ConfirmError as e:
        return fail(str(e), e.code)
    except (mattermost.HTTPError, OSError) as e:
        return fail(f"chat server error: {e}", 4)
    print(cid)
    return 0
