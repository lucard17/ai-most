"""The edge's loop arbiter and the bridge's side of it (delivery spec, "Agent loop guard"; Oracle: upstream
chat-loop-edge.test.sh): the chain, the signed mark, lifting by a person or by "continue", renewal, the bridge not waking
roles in a paused thread, the pin line."""

from __future__ import annotations

import json
import time
import types
import unittest

from helpers import IsolatedTest
from fakechat import Chat

from aimost import edge as edgemod
from aimost import link, looparb, waiting
from aimost.names import P

SECRET = "s" * 32


def world(free=""):
    c = Chat()
    t = c.team("t")
    ann, bob = c.user("ann"), c.user("bob")
    a = c.user("ann-dev", bot=True, owner=ann)
    b = c.user("bob-ops", bot=True, owner=bob)
    a2 = c.user("ann-qa", bot=True, owner=ann)
    for u in (a, b, a2, ann, bob):
        c.team_members.setdefault(t, set()).add(u)
    ch = c.channel(t, "work", "O", {a, b, a2, ann, bob})
    dm = c.channel(t, "x__y", "D", {a, b})
    fr = c.channel(t, "agents", "O", {a, b})
    e = edgemod.Edge(env={"EDGE_SECRET": SECRET, "EDGE_LOOP_FREE_CHANNELS": free, "EDGE_LOOP_EVERY_S": "20"}, call=c,
                     log=lambda m: None)
    return types.SimpleNamespace(c=c, ann=ann, bob=bob, a=a, b=b, a2=a2, ch=ch, dm=dm, free=fr, e=e,
                                 arb=looparb.Arbiter(e))


def ping_pong(w, channel, n=8, mention=True):
    """n bot posts: the first one calls, the rest are links (n - 1)."""
    root = w.c.post(channel, w.ann, "let's discuss")
    for i in range(n):
        who, other = (w.a, "bob-ops") if i % 2 == 0 else (w.b, "ann-dev")
        w.c.post(channel, who, f"@{other} what do you think?" if mention or i < 2 else "and so on", root=root)
    return root


class Chain(unittest.TestCase):
    def test_bots_of_two_people_answering_each_other(self):
        w = world()
        root = ping_pong(w, w.ch, 8)
        w.arb.pass_()
        mark = w.c.posts[root]["props"].get(P("loop_pause"))
        self.assertTrue(mark, "seven links — a pause")
        self.assertEqual((mark["authors"], mark["owners"], mark["links"]), (["ann-dev", "bob-ops"], ["ann", "bob"], 7))
        self.assertEqual(len([x for x in w.c.posts.values() if x["root_id"] == root]), 8, "no post in the thread")
        self.assertIn(root, w.arb._prefs(w.c.me, looparb.PAUSE_CAT))

    def test_six_links_are_not_a_loop(self):  # seven bot posts
        w = world()
        root = ping_pong(w, w.ch, 7)
        w.arb.pass_()
        self.assertNotIn(P("loop_pause"), w.c.posts[root]["props"])

    def test_without_mentions_only_bots_of_the_same_owner(self):
        w = world()
        root = w.c.post(w.ch, w.ann, "go")
        for i in range(9):     # two bots of ann: after the first mentions they keep answering without @
            who, other = (w.a, "ann-qa") if i % 2 == 0 else (w.a2, "ann-dev")
            w.c.post(w.ch, who, f"@{other} ok" if i < 2 else "ok", root=root)
        w.arb.pass_()
        self.assertTrue(w.c.posts[root]["props"].get(P("loop_pause")), "same owner: a talk partner counts without @")
        w2 = world()
        r2 = ping_pong(w2, w2.ch, 9, mention=False)
        w2.arb.pass_()
        self.assertNotIn(P("loop_pause"), w2.c.posts[r2]["props"], "bots of different people: only @ counts")

    def test_reports_to_people_are_not_a_loop(self):
        w = world()
        root = w.c.post(w.ch, w.ann, "deploy")
        for i in range(8):     # b reports to a, a never addresses b
            w.c.post(w.ch, w.b if i % 2 == 0 else w.a, "@ann-dev done" if i % 2 == 0 else "noted", root=root)
        w.arb.pass_()
        self.assertNotIn(P("loop_pause"), w.c.posts[root]["props"])

    def test_a_person_or_a_reaction_breaks_the_chain(self):
        w = world()
        root = ping_pong(w, w.ch, 5)
        w.c.post(w.ch, w.bob, "hm", root=root)
        for i in range(5):
            w.c.post(w.ch, w.a if i % 2 == 0 else w.b, "@bob-ops x" if i % 2 == 0 else "@ann-dev y", root=root)
        w.arb.pass_()
        self.assertNotIn(P("loop_pause"), w.c.posts[root]["props"])
        x = {"user_id": w.a, "props": {}, "metadata": {"reactions": [{"user_id": w.ann}]}}
        self.assertTrue(looparb.Arbiter.touched(x, {w.a: {}}))
        y = {"user_id": w.a, "props": {P("answers"): {"1": {"user_id": w.ann}}}}
        self.assertTrue(looparb.Arbiter.touched(y, {w.a: {}}))

    def test_dms_and_free_channels_are_not_read(self):
        w = world(free="agents")
        ping_pong(w, w.dm, 8)
        ping_pong(w, w.free, 8)
        w.arb.pass_()
        read = {p for m, p in w.c.calls if p.startswith("/channels/") and "/posts" in p}
        self.assertFalse(any(w.dm in p or w.free in p for p in read))
        self.assertTrue(any(w.ch in p for p in read))


class Mark(unittest.TestCase):
    def test_signature_and_age(self):
        w = world()
        root = ping_pong(w, w.ch, 8)
        w.arb.pass_()
        rp = w.c.posts[root]
        conn = {"EDGE_SECRET": SECRET}
        self.assertTrue(looparb.valid_mark(conn, rp))
        self.assertIsNone(looparb.valid_mark({"EDGE_SECRET": "other"}, rp), "a wrong key")
        forged = json.loads(json.dumps(rp))
        forged["props"][P("loop_pause")]["owners"] = ["eve"]
        self.assertIsNone(looparb.valid_mark(conn, forged), "a changed field breaks the signature")
        old = time.time() * 1000 + 2 * 86400 * 1000
        self.assertIsNone(looparb.valid_mark(conn, rp, now_ms=old), "not renewed for a day — ignored")
        self.assertIsNone(looparb.valid_mark({}, rp), "no key — nothing to check with")

    def test_a_mark_the_edge_did_not_set_is_removed(self):
        w = world()
        root = ping_pong(w, w.ch, 8)
        w.arb.pass_()
        mark = w.c.posts[root]["props"][P("loop_pause")]
        w.arb.clear()
        self.assertNotIn(P("loop_pause"), w.c.posts[root]["props"])
        w.c.posts[root]["props"][P("loop_pause")] = mark       # put back by the root's author
        w.c.post(w.ch, w.a, "@bob-ops more", root=root)
        w.arb.pass_()
        self.assertNotEqual(w.c.posts[root]["props"].get(P("loop_pause"), {}).get("id"), mark["id"])

    def test_renewed(self):
        w = world()
        root = ping_pong(w, w.ch, 8)
        w.arb.pass_()
        first = w.c.posts[root]["props"][P("loop_pause")]
        w.arb.pass_(now=time.time() + 7 * 3600)
        m = w.c.posts[root]["props"][P("loop_pause")]
        self.assertEqual(m["id"], first["id"])
        self.assertGreater(m["renewed"], first["renewed"])
        self.assertTrue(looparb.valid_mark({"EDGE_SECRET": SECRET}, w.c.posts[root], now_ms=m["renewed"]))


class Lifting(unittest.TestCase):
    def test_a_person_in_the_thread(self):
        w = world()
        root = ping_pong(w, w.ch, 8)
        w.arb.pass_()
        w.c.post(w.ch, w.bob, "stop it", root=root)
        w.arb.pass_()
        self.assertNotIn(P("loop_pause"), w.c.posts[root]["props"])
        self.assertNotIn(root, w.arb._prefs(w.c.me, looparb.PAUSE_CAT))

    def test_continue_only_by_an_owner(self):
        w = world()
        root = ping_pong(w, w.ch, 8)
        w.arb.pass_()
        eve = w.c.user("eve")
        self.assertIn("owner", w.arb.cont(root, eve))
        self.assertIn("owner", w.arb.cont(root, w.a), "a bot does not lift it")
        time.sleep(0.05)                 # the fake clock may run a few ms ahead of real time
        self.assertIsNone(w.arb.cont(root, w.bob))
        self.assertNotIn(P("loop_pause"), w.c.posts[root]["props"])
        self.assertIn(root, w.arb._prefs(w.c.me, looparb.CLEARED_CAT))
        self.assertEqual(w.arb.cont(root, w.bob), "this thread has no pause")
        self.assertEqual(w.arb.cont("x", w.bob), "not that thread")
        w.c.post(w.ch, w.a, "@bob-ops again", root=root)
        w.arb.pass_()
        self.assertNotIn(P("loop_pause"), w.c.posts[root]["props"], "after continue the chain counts from that moment")

    def test_continue_through_the_edge(self):
        w = world()
        root = ping_pong(w, w.ch, 8)
        w.arb.pass_()
        looparb._ARB[id(w.e)] = w.arb
        self.addCleanup(looparb._ARB.pop, id(w.e), None)
        body = {"user_id": w.bob, "post_id": "", "context": {"kind": "loop-continue", "root": root, "to": ["bob"],
                                                            "secret": SECRET}}
        code, r = w.e.handle("/action", json.dumps(body).encode())
        self.assertEqual(code, 200)
        self.assertIn("pause is lifted", r["ephemeral_text"])
        self.assertNotIn(P("loop_pause"), w.c.posts[root]["props"])


class Bridge(IsolatedTest):
    def setUp(self):
        super().setUp()
        self.w = world()
        self.root = ping_pong(self.w, self.w.ch, 8)
        self.w.arb.pass_()

    def link(self, owner="ann"):
        w = self.w
        x = link.Link.__new__(link.Link)
        x.c = {"NAME": "t", "OWNER": owner, "EDGE_SECRET": SECRET}
        x.name, x.threads, x.roots = "t", {}, {}
        x.api = lambda method, path, body=None, who=None: w.c(method, path, body)
        x.user = lambda uid: w.c.users[uid]
        x.ctx = types.SimpleNamespace(state=lambda s: self.tmp / f"t.{s}.json", link=lambda pid: f"/p/{pid}")
        return x

    def test_a_bot_post_in_a_paused_thread_wakes_nobody(self):
        x, w = self.link(), self.w
        later = w.c.posts[w.c.post(w.ch, w.b, "@ann-dev still?", root=self.root)]
        self.assertTrue(x.edge_paused(later, "dev"))
        person = w.c.posts[w.c.post(w.ch, w.ann, "@ann-dev ok go", root=self.root)]
        self.assertFalse(x.edge_paused(person, "dev"), "a person's post is delivered")
        x.threads = {}
        after = w.c.posts[w.c.post(w.ch, w.b, "@ann-dev here", root=self.root)]
        self.assertFalse(x.edge_paused(after, "dev"), "a person wrote after the mark — lifted here at once")
        w.c.posts[self.root]["props"][P("loop_pause")]["sigs"] = {"*": "0" * 64}
        x.roots, x.threads = {}, {}
        self.assertFalse(x.edge_paused(later, "dev"), "a forged mark is ignored")

    def test_on_post_does_not_deliver_a_paused_thread(self):
        x, w = self.link(), self.w
        got = []
        x.ids, x.handle, x.relay, x.delivered, x.unrouted = {"dev": w.a}, {"dev": "ann-dev"}, "", {}, {}
        x.role = lambda uid, who: "peer"
        x.relevant = lambda p, who, ch: "mention"
        x.loop_guard = lambda *a, **k: False
        x.put = lambda to, text, **kw: got.append(kw["post"])
        x.react = lambda *a, **k: None
        paused = w.c.posts[w.c.post(w.ch, w.b, "@ann-dev more", root=self.root)]
        x.on_post(paused, "dev", {}, {"name": "work", "type": "O"}, 0)
        self.assertEqual(got, [], "paused: not delivered")
        free = w.c.posts[w.c.post(w.ch, w.b, "@ann-dev elsewhere")]
        x.on_post(free, "dev", {}, {"name": "work", "type": "O"}, 0)
        self.assertEqual(got, [free["id"]])

    def test_the_owner_gets_a_pin_line_with_continue(self):
        x = self.link("ann")
        x.loop_note(self.w.c.posts[self.root])
        loops = json.loads((self.tmp / "t.loops.json").read_text())
        self.assertEqual(list(loops), [self.root])
        ctx = types.SimpleNamespace(c={"NAME": "t", "OWNER": "ann", "EDGE_PUBLIC": "edge:1", "EDGE_SECRET": SECRET},
                                    name="t", state=lambda s: self.tmp / f"t.{s}.json", link=lambda pid: f"/p/{pid}")
        rows = [r for r in waiting.waiting_rows(ctx) if r.get("loop")]
        self.assertEqual([r["key"] for r in rows], ["loop:" + self.root])
        _, atts = waiting.render_pin(ctx, rows, [], time.time() * 1000)
        acts = [a for at in atts for a in at["actions"] if a["name"] == "continue"]
        self.assertEqual(acts[0]["integration"]["context"]["kind"], "loop-continue")
        self.assertEqual(acts[0]["integration"]["context"]["root"], self.root)
        y = self.link("carol")          # not among the mark's owners
        y.loop_note(self.w.c.posts[self.root])
        self.assertEqual(json.loads((self.tmp / "t.loops.json").read_text()), {}, "not mine — the line goes")


if __name__ == "__main__":
    unittest.main()
