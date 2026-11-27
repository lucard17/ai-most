"""``lmost activity`` — what an agent is doing, shown in the chat (called by hooks, the bridge and the dispatcher).

  lmost activity begin <role> <event-json>   start a chat turn (inbox event: via, thread|post, text, from, role, channel)
  lmost activity step <role> ["<desc>"]      a step of the turn (empty — counted, not shown)
  lmost activity end <role>                  end of the turn: outcome into the progress post, ✅ on its posts
  lmost activity tick                        "typing…" for all running turns + delayed progress
  lmost activity status-sync < agents.json   bot statuses from `claude agents --json`
  lmost activity waiting-notify < agents.json  a session waiting for a person — one direct message to the owner
  lmost activity react <via> <role> <post> <emoji>
"""

from __future__ import annotations

import json
import sys
from typing import List

from . import activity


def main(argv: List[str]) -> int:
    a = argv
    try:
        if a[:1] == ["begin"] and len(a) == 3:
            activity.begin(a[1], json.loads(a[2]))
        elif a[:1] == ["step"] and len(a) in (2, 3):
            activity.step(a[1], a[2] if len(a) > 2 else "")
        elif a[:1] == ["end"] and len(a) == 2:
            activity.end(a[1])
        elif a == ["tick"]:
            activity.tick()
        elif a == ["status-sync"]:
            activity.status_sync(json.load(sys.stdin))
        elif a == ["waiting-notify"]:
            activity.waiting_notify(json.load(sys.stdin))
        elif a[:1] == ["react"] and len(a) == 5:
            activity.react_now(*a[1:])
        else:
            print(__doc__)
            return 2
    except Exception as e:
        activity.log(f"{a[:1]}: {e}")
        return 1
    return 0
