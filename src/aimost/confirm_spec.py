"""What a confirmation says, as fields (authority spec, "a confirmation describes the action").

Why. A confirmation made of raw commands ("glab mr merge 467; ssh … restart") tells the owner nothing about what will
happen: what changes, what does not, how to undo it. So the meaning is in required fields, the commands go into a field
of their own that is not posted, and a bare command in a meaning field is refused before anything is posted.

    {"do": "what I do — one sentence",            required, up to 160 characters
     "why": "why, and at whose request",            required, up to 300
     "steps": ["what happens — a step", …],         required, 1–7 steps, up to 200 each
     "keeps": "what does not change",               required, up to 300
     "rollback": "how to undo it | irreversible: …", required, up to 300
     "mrs": ["repo!N", …],                          merge requests the action is about (a card each in the post)
     "merges": ["repo!N", …],                       merge requests the action MERGES (also in mrs): only their merge is
                                                    covered by the "yes"
     "commands": ["…", …]}                          kept with the confirmation, not posted
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

LIMITS = {"do": 160, "why": 300, "keeps": 300, "rollback": 300}
STEP_MAX, STEPS_MAX, MRS_MAX = 200, 7, 10
REQUIRED = ("do", "why", "keeps", "rollback")
MR = re.compile(r"[a-z0-9][a-z0-9_.-]*[!#][0-9]+")       # GitLab repo!N, GitHub repo#N
# An executable at the start of a phrase, after a command separator or a back-tick, followed by an argument. One name
# without arguments ("via `glab`", "`bin/x.sh`") is the subject of the action, not a command; a command in the middle of a
# sentence ("merging through glab mr merge") is not caught on purpose — a false refusal costs more than a miss, and the
# owner sees the post anyway. "plan", "node" and the like are ordinary words and are not in the list.
EXE = (r"glab|gh|git|ssh|scp|rsync|docker|podman|ansible-playbook|ansible|systemctl|journalctl|psql|mysql|curl|wget|bash|"
       r"sh|zsh|python3?|npm|npx|make|sudo|rm|mv|cp|cd|kill|pkill|terraform|tofu|kubectl|helm|jq|sed|awk|aimost|mmctl")
CMD = re.compile(r"(?:^|[;`]\s*|&&\s*|\|\|?\s*|\$\(\s*)((?:" + EXE + r")|(?:[\w./~-]*/)?bin/[\w.-]+)(?=[ \t]+[^\s`])")
FLAG = re.compile(r"(?<![\w/.-])(--[a-z][\w-]+|-[a-zA-Z])(?=[\s`=]|$)")
CHAIN = re.compile(r"&&")


def _as_list(v, name: str, err: List[str]) -> list:
    """A string — one item; a list — as is; anything else — an error naming the field."""
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    if isinstance(v, list):
        return v
    err.append(f"{name}: a list of strings expected, not {type(v).__name__}")
    return []


def _refs(v, name: str, err: List[str]) -> List[str]:
    return [str(x).strip() for x in _as_list(v, name, err) if str(x).strip()]


def commandish(text: str) -> Optional[str]:
    """The first fragment that looks like a shell command, or None."""
    t = text or ""
    m = CMD.search(t) or FLAG.search(t) or CHAIN.search(t)
    return t[m.start():m.start() + 40].strip() if m else None


def check(spec) -> Tuple[Optional[dict], List[str]]:
    """(the normalized description, [errors]) — errors ready to show: "field: what is wrong"."""
    if not isinstance(spec, dict):
        return None, ["the description is a JSON object with the fields do, why, steps, keeps, rollback"]
    err: List[str] = []
    out: dict = {}
    for k in REQUIRED:
        v = spec.get(k)
        v = " ".join(v.split()) if isinstance(v, str) else ""      # a line break would break the post's layout
        if not v:
            err.append(f"no field {k}" + (" — how to undo it, or «irreversible: <why>»" if k == "rollback" else ""))
            continue
        if len(v) > LIMITS[k]:
            err.append(f"{k}: {len(v)} characters, limit {LIMITS[k]}")
        out[k] = v
    steps = [" ".join(s.split()) for s in _as_list(spec.get("steps"), "steps", err) if isinstance(s, str) and s.strip()]
    if not steps:
        err.append("no field steps — what happens after the press, step by step")
    if len(steps) > STEPS_MAX:
        err.append(f"steps: {len(steps)} steps, limit {STEPS_MAX}")
    for i, s in enumerate(steps, 1):
        if len(s) > STEP_MAX:
            err.append(f"steps[{i}]: {len(s)} characters, limit {STEP_MAX}")
    out["steps"] = steps
    for k, v in [(k, out.get(k, "")) for k in REQUIRED] + [(f"steps[{i}]", s) for i, s in enumerate(steps, 1)]:
        c = commandish(v)
        if c:
            err.append(f"{k}: a command «{c}» — commands go into the field commands, here — what happens, in words")
    mrs, merges = _refs(spec.get("mrs"), "mrs", err), _refs(spec.get("merges"), "merges", err)
    for x in mrs + merges:
        if not MR.fullmatch(x):
            err.append(f"merge request «{x}» — <repo>!<N> (or <repo>#<N>) expected")
    out["merges"] = list(dict.fromkeys(merges))
    out["mrs"] = list(dict.fromkeys(mrs + merges))
    if len(out["mrs"]) > MRS_MAX:
        err.append(f"mrs: {len(out['mrs'])} merge requests, limit {MRS_MAX}")
    out["commands"] = [str(x) for x in _as_list(spec.get("commands"), "commands", err)]
    unknown = sorted(set(spec) - set(REQUIRED) - {"steps", "mrs", "merges", "commands"})
    if unknown:
        err.append("unknown fields: " + ", ".join(unknown))
    return out, err
