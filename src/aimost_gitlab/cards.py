"""Merge request cards in a confirmation post (authority spec, "a confirmation describes the action"): title, state, the
head commit and its pipeline — from GitLab, so the owner presses ✅ on what the merge request is now, not on what the
agent remembers. Only ``repo!N`` in the connection's ``GITLAB_GROUP``; GitLab silent or slow — no card (the post says
"not checked"), never a failure of the confirmation."""

from __future__ import annotations

import re
import urllib.parse
from typing import Optional

from . import glab

REF = re.compile(r"([a-z0-9][a-z0-9_.-]*)!([0-9]+)")


def card(ref: str, conn: dict) -> Optional[dict]:
    m = REF.fullmatch(ref or "")
    grp = glab.group(conn)
    if not m or not grp or not glab.available():
        return None
    repo, n = m.groups()
    mr = glab.api(f"projects/{urllib.parse.quote(f'{grp}/{repo}', safe='')}/merge_requests/{n}", timeout=20)
    if not isinstance(mr, dict) or not mr.get("iid"):
        return None
    st = mr.get("state")
    sha = ((mr.get("merge_commit_sha") if st == "merged" else None) or mr.get("sha") or "")[:8]
    pipe = mr.get("head_pipeline") or {}
    ci = pipe.get("status") if pipe and (pipe.get("sha") or mr.get("sha")) == mr.get("sha") else None
    state = st if st in ("merged", "closed") else ("draft" if mr.get("draft") or mr.get("work_in_progress") else "open")
    return {"ref": ref, "url": mr.get("web_url") or "", "title": " ".join((mr.get("title") or "").split()),
            "state": state, "sha": sha, "ci": ci}
