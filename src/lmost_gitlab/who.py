"""GitLab users for ``lmost who``."""

from __future__ import annotations

import urllib.parse
from typing import Dict, List

from . import glab


def search(term: str) -> List[Dict[str, str]]:
    if not glab.available():
        return []
    users = glab.api(f"users?search={urllib.parse.quote(term)}&per_page=20", timeout=30) or []
    return [{"source": "GitLab", "login": u.get("username", ""), "name": u.get("name", "")}
            for u in users if u.get("username")]
