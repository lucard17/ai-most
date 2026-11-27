"""MR references in outgoing text (gitlab-mr spec, "MR references become links").

``repo!N`` → ``[repo!N](url) «title»``; a link ``[repo!N](url)`` without the title on its line gets the title; ``repo !N``
(the repository as the word right before ``!N``) is treated as ``repo!N`` when GitLab knows that MR; a bare ``!N`` is
refused with code 20 (almost every repository has an MR with that number). Code spans and blocks are not touched.
GitLab unreachable, or no ``GITLAB_GROUP`` — the text goes as is (a note on stderr when GitLab is down); no ``glab`` —
the check does nothing.
"""

from __future__ import annotations

import re
import urllib.parse
from typing import Dict, List, Optional, Tuple

from aimost import textcheck

from . import glab

CODE_REFUSED = 20
MAX_LOOKUPS = 10                      # a post does not wait for GitLab on each of a hundred numbers
CODE = re.compile(r"(```.*?```|`[^`\n]*`)", re.S)
MR_LINK = re.compile(r"\[([a-z][a-z0-9_.-]*)!(\d+)\]\(([^)\s]+)\)")
MR_BARE = re.compile(r"(?<![\w/\[.!-])([a-z][a-z0-9_.-]*)!(\d+)(?![\d\]])")
NO_REPO = re.compile(r"(?<![\w!\]\)\[/-])!(\d+)\b")
REPO_SP = re.compile(r"(?<![\w/\[.!-])([a-z][a-z0-9_.-]*) +!(\d+)\b")


class Lookup:
    """MR titles for one post: cached, bounded, and stopped at the first GitLab failure."""

    def __init__(self, group: str):
        self.group = group
        self.cache: Dict[Tuple[str, str], Optional[Tuple[str, str]]] = {}
        self.down: List[str] = []

    def __call__(self, repo: str, n: str) -> Optional[Tuple[str, str]]:
        k = (repo, n)
        if k in self.cache:
            return self.cache[k]
        self.cache[k] = None
        if self.down or not self.group or len(self.cache) > MAX_LOOKUPS:
            return None
        try:
            enc = urllib.parse.quote(f"{self.group}/{repo}", safe="")
            j = glab.api(f"projects/{enc}/merge_requests/{n}", timeout=15) or {}
            self.cache[k] = (" ".join((j.get("title") or "").split()), j.get("web_url") or "")
        except glab.NotFound:
            pass
        except Exception as x:
            self.down.append(str(x)[:120])
        return self.cache[k]


def _titled(line: str, title: str) -> bool:
    return bool(title) and title.lower()[:40] in line.lower()


def _line_of(pos: int, s: str) -> str:
    a = s.rfind("\n", 0, pos) + 1
    b = s.find("\n", pos)
    return s[a:b if b >= 0 else len(s)]


def fill(text: str, info: Lookup, bare_hits: List[str]) -> str:
    if not text:
        return text
    parts = CODE.split(text)
    for i in range(0, len(parts), 2):                       # odd parts are code — untouched
        seg = REPO_SP.sub(lambda m: f"{m.group(1)}!{m.group(2)}" if info(m.group(1), m.group(2)) else m.group(0),
                          parts[i])
        check = MR_LINK.sub(" ", seg)
        if info.down or not info.group:                      # cannot tell "repo !N" from a bare number — not refused
            check = REPO_SP.sub(" ", check)
        bare_hits.extend(m.group(1) for m in NO_REPO.finditer(check))

        def link(m, seg=seg):
            got = info(m.group(1), m.group(2))
            if not got or _titled(_line_of(m.start(), seg), got[0]):
                return m.group(0)
            return f"{m.group(0)} «{got[0]}»"
        seg = MR_LINK.sub(link, seg)

        def bare(m, seg=seg):
            got = info(m.group(1), m.group(2))
            if not got or not got[1]:
                return m.group(0)
            ref = f"[{m.group(1)}!{m.group(2)}]({got[1]})"
            return ref if _titled(_line_of(m.start(), seg), got[0]) else f"{ref} «{got[0]}»"
        out, pos = [], 0                                     # bare refs — outside the ready links
        for lm in MR_LINK.finditer(seg):
            out.append(MR_BARE.sub(bare, seg[pos:lm.start()]))
            out.append(lm.group(0))
            pos = lm.end()
        out.append(MR_BARE.sub(bare, seg[pos:]))
        parts[i] = "".join(out)
    return "".join(parts)


def check(d: textcheck.Draft) -> None:
    """The text check: rewrite MR references, refuse bare ``!N``. Without ``glab`` — nothing (the plugin is optional)."""
    if not glab.available():
        return
    info = Lookup(glab.group(d.conn))
    hits: List[str] = []
    d.map_text(lambda t: fill(t, info, hits))
    if hits:
        nums = list(dict.fromkeys(hits))
        raise textcheck.Refused(CODE_REFUSED, "MR without a repository: " + ", ".join("!" + n for n in nums)
                                + " — almost every repository has an MR with that number; name the repository: "
                                + ", ".join(f"<repo>!{n}" for n in nums) + " (the link and title are added for you)")
    if info.down:
        d.notes.append(f"GitLab unreachable — MR references left as they are ({info.down[0]})")
