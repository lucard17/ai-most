# l-most

A bridge between Claude Code sessions and a Mattermost server, built for teams where several AI agents work next to people
in the same chat.

- every agent role has its own bot — real authors, mentions and threads work;
- chat posts, mentions, thread replies and reactions land in the session's inbox and reach the session mid-turn or at
  the end of the turn; idle sessions are woken up;
- the sender's role matters: the owner's words carry authority, another agent's or a colleague's do not — a turn started
  by a peer is read-only;
- questions with buttons, confirmation ✅/❌ before dangerous actions, several chat servers, GitLab MR events.

**Status: planning.** The working version runs privately; it is being ported here. See
[docs/decisions.md](docs/decisions.md) and [docs/port-map.md](docs/port-map.md).

License: MIT.
