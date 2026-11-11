"""Resolve which X queries and subreddits the social collectors use.

Reddit and X are fetched globally (one shared fetch / one browser collector),
so the query set is the union of every searchable user's profile-generated
`search_seeds.social` block (profile_builder rule 24c). The profile builder
LLM decides what to search for; this module only merges, de-duplicates and
enforces the wire-format bounds the extension protocol accepts. Profiles
without the block (built before 2026-11-11) contribute nothing; when no user
has one, the generic DEFAULTS lists are used.
"""
from __future__ import annotations

import logging
import re
from typing import Iterable

log = logging.getLogger(__name__)

# Protocol bounds (social_browser.collection_config / chrome-extension/protocol.js).
MAX_X_QUERIES = 12
MAX_SUBREDDITS = 50
_MAX_X_QUERY = 500
_SUBREDDIT_RE = re.compile(r"[A-Za-z0-9_]{1,21}")


def profile_social_seeds(profile: dict | None) -> tuple[list[str], list[str]]:
    """Return (x_queries, subreddits) from one profile, validated for transport."""
    seeds = (profile or {}).get("search_seeds") if isinstance(profile, dict) else None
    social = seeds.get("social") if isinstance(seeds, dict) else None
    if not isinstance(social, dict):
        return [], []
    queries = [q.strip() for q in social.get("x_queries") or []
               if isinstance(q, str) and q.strip() and len(q.strip()) <= _MAX_X_QUERY]
    subs = [s.strip().removeprefix("r/") for s in social.get("subreddits") or []
            if isinstance(s, str)]
    return queries, [s for s in subs if _SUBREDDIT_RE.fullmatch(s)]


def merge_social_seeds(profiles: Iterable[dict | None]) -> tuple[list[str], list[str]]:
    """Round-robin merge so every user gets queries in before the caps bite."""
    per_user = [profile_social_seeds(p) for p in profiles]
    queries: list[str] = []
    subs: list[str] = []
    seen_q: set[str] = set()
    seen_s: set[str] = set()
    depth = max((max(len(q), len(s)) for q, s in per_user), default=0)
    for i in range(depth):
        for user_queries, user_subs in per_user:
            if i < len(user_queries) and len(queries) < MAX_X_QUERIES:
                key = " ".join(user_queries[i].lower().split())
                if key not in seen_q:
                    seen_q.add(key)
                    queries.append(user_queries[i])
            if i < len(user_subs) and len(subs) < MAX_SUBREDDITS:
                key = user_subs[i].lower()
                if key not in seen_s:
                    seen_s.add(key)
                    subs.append(user_subs[i])
    return queries, subs


def resolve(db, *, chat_ids: Iterable[int] | None = None, defaults: dict | None = None) -> dict:
    """Return {"x_search_queries": [...], "reddit_subreddits": [...]}.

    `chat_ids=None` means every user `db.users_for_search()` returns. Each
    list falls back to `defaults` independently when no profile supplies it.
    Never raises: any DB/profile failure degrades to the defaults.
    """
    if defaults is None:
        from defaults import DEFAULTS as defaults
    fallback = {
        "x_search_queries": list(defaults.get("x_search_queries") or []),
        "reddit_subreddits": list(defaults.get("reddit_subreddits") or []),
    }
    if db is None:
        return fallback
    try:
        from user_profile import profile_from_json

        if chat_ids is None:
            raws = [row["user_profile"] for row in db.users_for_search()]
        else:
            raws = [db.get_user_profile(cid) for cid in chat_ids]
        queries, subs = merge_social_seeds(profile_from_json(raw) for raw in raws)
    except Exception:
        log.exception("social_seeds: profile resolution failed; using defaults")
        return fallback
    return {
        "x_search_queries": queries or fallback["x_search_queries"],
        "reddit_subreddits": subs or fallback["reddit_subreddits"],
    }


def to_api_query(query: str) -> str:
    """Translate X web-search operators to their API v2 recent-search spelling.

    Profiles and DEFAULTS store web syntax (what the browser collector types
    into x.com/search); the API rejects `filter:` operators.
    """
    out = re.sub(r"(?<!\S)-filter:retweets(?!\S)", "-is:retweet", query)
    out = re.sub(r"(?<!\S)-filter:replies(?!\S)", "-is:reply", out)
    return out
