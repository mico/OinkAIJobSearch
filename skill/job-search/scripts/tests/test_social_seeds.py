"""Profile-driven Reddit/X targeting (search_seeds.social, profile_builder rule 24c)."""
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import profile_builder as pb
import social_seeds as ss

DEFAULTS = {"x_search_queries": ["generic"], "reddit_subreddits": ["forhire"]}


def prof(queries=(), subs=()):
    return {"search_seeds": {"social": {"x_queries": list(queries), "subreddits": list(subs)}}}


class FakeDB:
    def __init__(self, profiles: dict):
        self.profiles = profiles

    def users_for_search(self):
        return [{"user_profile": json.dumps(p) if p is not None else None}
                for p in self.profiles.values()]

    def get_user_profile(self, cid):
        p = self.profiles.get(cid)
        return json.dumps(p) if p is not None else None


def test_profile_without_block_falls_back_to_defaults():
    out = ss.resolve(FakeDB({1: {"search_seeds": {}}}), defaults=DEFAULTS)
    assert out == DEFAULTS


def test_no_db_uses_defaults():
    assert ss.resolve(None, defaults=DEFAULTS) == DEFAULTS


def test_single_user_seeds_replace_defaults():
    db = FakeDB({1: prof(['"we\'re hiring" rails'], ["rustjobs", "r/golangjobs"])})
    out = ss.resolve(db, chat_ids=[1], defaults=DEFAULTS)
    assert out == {"x_search_queries": ['"we\'re hiring" rails'],
                   "reddit_subreddits": ["rustjobs", "golangjobs"]}


def test_each_list_falls_back_independently():
    out = ss.resolve(FakeDB({1: prof(["q1"], [])}), defaults=DEFAULTS)
    assert out == {"x_search_queries": ["q1"], "reddit_subreddits": ["forhire"]}


def test_chat_ids_scope_to_requested_user():
    db = FakeDB({1: prof(["a"], ["s1"]), 2: prof(["b"], ["s2"])})
    assert ss.resolve(db, chat_ids=[2], defaults=DEFAULTS)["x_search_queries"] == ["b"]


def test_union_round_robin_dedupes_and_caps():
    a = prof([f"a{i}" for i in range(10)], ["Hiring", "rustjobs"])
    b = prof([f"b{i}" for i in range(10)], ["hiring", "golangjobs"])
    queries, subs = ss.merge_social_seeds([a, b])
    assert len(queries) == ss.MAX_X_QUERIES
    assert queries[:4] == ["a0", "b0", "a1", "b1"]  # fair interleave
    assert subs == ["Hiring", "rustjobs", "golangjobs"]  # case-insensitive dedupe


def test_invalid_entries_dropped_for_transport():
    q, s = ss.profile_social_seeds(prof(["ok", "", 5, "x" * 600], ["good_one", "bad/name", "a b"]))
    assert q == ["ok"] and s == ["good_one"]


def test_db_failure_degrades_to_defaults():
    class Broken:
        def users_for_search(self):
            raise RuntimeError("locked")
    assert ss.resolve(Broken(), defaults=DEFAULTS) == DEFAULTS


@pytest.mark.parametrize("web,api", [
    ("backend -filter:retweets -filter:replies", "backend -is:retweet -is:reply"),
    ('"we\'re hiring" rails', '"we\'re hiring" rails'),
])
def test_to_api_query(web, api):
    assert ss.to_api_query(web) == api


def test_defaults_use_web_syntax():
    from defaults import DEFAULTS as D
    assert all("is:" not in q and "-filter:retweets" in q for q in D["x_search_queries"])


def test_validator_accepts_and_rejects_social_block():
    assert pb._validate_social_seeds(None) == []
    assert pb._validate_social_seeds({"x_queries": ["q"], "subreddits": ["rustjobs"]}) == []
    assert pb._validate_social_seeds({"x_queries": ["q"], "subreddits": ["bad/name"]})
    assert pb._validate_social_seeds({"x_queries": ["x" * 301]})
    assert pb._validate_social_seeds("nope")


def test_clip_trims_social_lists():
    p = {"search_seeds": {"social": {"x_queries": ["q"] * 9, "subreddits": ["s"] * 15}}}
    social = pb._clip_profile(p)["search_seeds"]["social"]
    assert len(social["x_queries"]) == pb._MAX_X_QUERIES
    assert len(social["subreddits"]) == pb._MAX_SUBREDDITS


def test_prompt_renders_with_social_rule():
    rendered = pb._render_prompt("resume", "prefs")
    assert "24c. `search_seeds.social`" in rendered
    assert '"social": {{' in rendered  # template keeps doubled braces verbatim


def test_scoring_prompt_covers_direct_hiring_posts():
    import job_enrich
    assert "DIRECT HIRING POSTS" in job_enrich._PROMPT
    assert "REPOSTS by job boards" in job_enrich._PROMPT


def test_x_api_receives_api_syntax(monkeypatch):
    import forensic
    from sources import x_twitter as x, _detail_fetch

    seen = []

    class Resp:
        status_code = 200
        def json(self):
            return {"data": []}

    monkeypatch.setenv("X_BEARER_TOKEN", "t")
    monkeypatch.delenv("OINK_SOCIAL_BROWSER_ENABLED", raising=False)
    monkeypatch.setattr(forensic, "log_step", lambda *a, **k: None)
    monkeypatch.setattr(_detail_fetch, "fetch_many_bodies", lambda *a, **k: {})
    monkeypatch.setattr(x.requests, "get", lambda url, **kw: seen.append(kw["params"]["query"]) or Resp())
    x.fetch({"x_search_queries": ["rails -filter:retweets -filter:replies"]})
    assert seen == ["rails -is:retweet -is:reply"]
