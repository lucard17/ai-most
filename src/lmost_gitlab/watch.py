"""MR → session link and MR activity into the session's inbox (gitlab-mr spec).

The link is written by the PostToolUse hook (``hook``) after a successful MR-creating command, or by hand (``add``).
``once`` (a timer, every few minutes) polls each linked MR and puts into the session's inbox:

* ✅ / ↩️ approval given or removed (``/approvals``);
* 💬 new notes by people — not system notes and not the account ``glab`` runs as (sessions write as their owner,
  otherwise the author would wake itself);
* 🔴 a failed pipeline on the MR's HEAD, once per sha (a green one does not come);
* 🏁 merged / closed — then the MR is unlinked.

The first poll of an MR (and ``add``) takes its current state as seen: what happened before linking does not come. The
"seen" mark moves after each delivered event, so an inbox failure mid-poll does not repeat what was delivered.

State — ``LMOST_GITLAB_DIR`` (``<state>/gitlab``)/``watch.json``, under flock: the hook and the timer write at once.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import urllib.parse
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from lmost import connections, paths, plugins

from . import NAME, glab

URL_RE = re.compile(r"^https?://[^/]+/(.+)/-/merge_requests/(\d+)")
REF_RE = re.compile(r"^([A-Za-z0-9_.-]+)!(\d+)$")


class WatchError(Exception):
    pass


def state_dir() -> Path:
    return Path(os.environ.get("LMOST_GITLAB_DIR") or paths.state_dir() / "gitlab")


def parse(target: str, project: str = "", conn: Optional[dict] = None) -> Tuple[str, int, str]:
    """url | repo!N → (group/repo, iid, repo!N)."""
    m = URL_RE.match(target)
    if m:
        proj, iid = m.group(1), int(m.group(2))
    else:
        m = REF_RE.match(target)
        if not m:
            raise WatchError(f"not an MR: {target!r} — give a URL or repo!N")
        grp = glab.group(conn)
        if not project and not grp:
            raise WatchError(f"{target}: no project — give --project <group/repo> or set GITLAB_GROUP")
        proj, iid = project or f"{grp}/{m.group(1)}", int(m.group(2))
    return proj, iid, f"{proj.split('/')[-1]}!{iid}"


class State:
    def __enter__(self) -> "State":
        d = paths.private_dir(state_dir())
        self.path = d / "watch.json"
        self.lock = open(str(self.path) + ".lock", "w")
        fcntl.flock(self.lock, fcntl.LOCK_EX)
        try:
            self.d = json.loads(self.path.read_text())
        except (OSError, ValueError):
            self.d = {}
        self.d.setdefault("mrs", {})
        return self

    def save(self) -> None:
        tmp = self.path.with_name("watch.json.tmp")
        tmp.write_text(json.dumps(self.d, ensure_ascii=False, indent=1))
        os.replace(tmp, self.path)

    def __exit__(self, *a) -> None:
        self.lock.close()


def me(st: State) -> str:
    if not st.d.get("me"):
        st.d["me"] = (glab.api("user") or {}).get("username")
    return st.d["me"]


def _enc(e: dict) -> str:
    return urllib.parse.quote(e["project"], safe="")


def notes_after(e: dict, after: int) -> List[dict]:
    """Notes with id > after (newest first, page by page: a day of system notes is more than a page)."""
    out: List[dict] = []
    for page in range(1, 21):
        chunk = glab.api(f"projects/{_enc(e)}/merge_requests/{e['iid']}/notes?sort=desc&order_by=created_at"
                         f"&per_page=100&page={page}") or []
        out += [n for n in chunk if n["id"] > after]
        if len(chunk) < 100 or min(n["id"] for n in chunk) <= after:
            break
    return out


def snapshot(e: dict, mr: dict, after: int) -> dict:
    appr = glab.api(f"projects/{_enc(e)}/merge_requests/{e['iid']}/approvals") or {}
    notes = notes_after(e, after)
    return {"approved": sorted(a["user"]["username"] for a in appr.get("approved_by") or []),
            "note_id": max([n["id"] for n in notes] or [after]), "failed_sha": None, "state": mr.get("state"),
            "title": mr.get("title"), "url": mr.get("web_url"), "_notes": notes}


Put = Callable[[str, str, str], None]


def inbox_put(session: str, sender: str, text: str) -> None:
    """Into the session's inbox as a source: the sender role is the core's (``peer`` unless SOURCES_OWNER)."""
    plugins.SourceInbox(NAME, session, connections.own_connection(session)).put(text, sender=sender, kind="gitlab")


def poll(ref: str, e: dict, me_name: str, put: Put = inbox_put) -> bool:
    """One MR: events into the inbox; True — merged, closed or gone: unlink it."""
    seen = e.get("seen")
    try:
        mr = glab.api(f"projects/{_enc(e)}/merge_requests/{e['iid']}") or {}
        cur = snapshot(e, mr, (seen or {}).get("note_id", 0))
    except glab.Gone as ex:
        if seen:
            put(e["session"], "gitlab", f"⚠️ [{ref}]({seen.get('url') or ''}) cannot be opened any more ({ex}) — unlinked")
        return True
    notes = cur.pop("_notes")
    if not seen:
        e["seen"] = cur
        return False
    head = f"[{ref}]({cur['url']}) «{cur['title']}»"
    sess = e["session"]
    seen["title"], seen["url"] = cur["title"], cur["url"]
    for u in sorted(set(cur["approved"]) - set(seen["approved"])):
        put(sess, u, f"✅ @{u} approved {head}")
        seen["approved"] = sorted(set(seen["approved"]) | {u})
    for u in sorted(set(seen["approved"]) - set(cur["approved"])):
        put(sess, u, f"↩️ @{u} removed the approval of {head}")
        seen["approved"] = sorted(set(seen["approved"]) - {u})
    for n in sorted(notes, key=lambda n: n["id"]):
        u = (n.get("author") or {}).get("username", "?")
        if not n.get("system") and u != me_name:
            where = " (on a line of code)" if n.get("position") else ""
            put(sess, u, f"💬 @{u} on {head}{where}: {(n.get('body') or '')[:800]}")
        seen["note_id"] = max(seen["note_id"], n["id"])
    hp = mr.get("head_pipeline") or {}
    if hp.get("status") == "failed" and hp.get("sha") == mr.get("sha") and seen.get("failed_sha") != hp.get("sha"):
        put(sess, "gitlab", f"🔴 pipeline failed on {head}, sha {hp['sha'][:9]}: {hp.get('web_url', '')}")
        seen["failed_sha"] = hp["sha"]
    done = cur["state"] in ("merged", "closed")
    if done and seen.get("state") != cur["state"]:
        by = (mr.get("merged_by") if cur["state"] == "merged" else mr.get("closed_by")) or {}
        put(sess, by.get("username", "gitlab"), f"🏁 {head} — {cur['state']}"
            + (f" (@{by['username']})" if by.get("username") else ""))
    seen["state"] = cur["state"]
    return done


def switched_on(session: str) -> bool:
    return plugins.enabled(NAME, connections.own_connection(session))


def add(target: str, session: str, project: str = "") -> str:
    """Link an MR to a session; takes its current state as seen. Returns repo!N."""
    proj, iid, ref = parse(target, project, connections.own_connection(session))
    with State() as st:
        prev = st.d["mrs"].get(ref)
        e = {"project": proj, "iid": iid, "session": session}
        if prev and prev.get("project") == proj:
            e["seen"] = prev.get("seen")
        if not e.get("seen"):
            try:
                if poll(ref, e, me(st)):
                    raise WatchError(f"{ref} cannot be opened (404/403) — not linked")
            except WatchError:
                raise
            except Exception:
                pass                                  # network: the first `once` takes the snapshot
        st.d["mrs"][ref] = e
        st.save()
    return ref


def remove(ref: str) -> bool:
    with State() as st:
        gone = st.d["mrs"].pop(ref, None)
        st.save()
    return gone is not None


def links() -> Dict[str, dict]:
    with State() as st:
        return dict(st.d["mrs"])


def once(log: Callable[[str], None] = lambda m: None) -> int:
    """Poll every linked MR; returns 1 if some failed. Sessions with the plugin switched off are skipped."""
    bad = 0
    with State() as st:
        if not st.d["mrs"]:
            return 0
        try:
            me_name = me(st)
        except Exception as ex:
            log(f"glab api user — {ex}")
            return 1
        for ref in sorted(st.d["mrs"]):
            e = st.d["mrs"][ref]
            if not switched_on(e["session"]):
                continue
            try:
                if poll(ref, e, me_name):
                    del st.d["mrs"][ref]
            except Exception as ex:
                bad = 1
                log(f"{ref}: {ex}")
            st.save()
    return bad
