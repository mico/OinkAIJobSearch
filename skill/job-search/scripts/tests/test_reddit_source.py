"""Reddit transport, structural filtering and global delivery integration."""
from datetime import datetime
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import forensic
from dedupe import Job, dedupe_cross_source
from sources import reddit, _detail_fetch
from sources._social import first_external_link, is_external_link


class FakeResp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._p = payload
        self.text = text
        self.ok = 200 <= status < 300

    def json(self):
        return self._p

    def raise_for_status(self):
        if not self.ok:
            raise reddit.requests.HTTPError(str(self.status_code))


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    for key in ("REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(forensic, "log_step", lambda *a, **k: None)
    monkeypatch.setattr(_detail_fetch, "fetch_many_bodies", lambda *a, **k: {})
    def unexpected(*a, **k):
        raise AssertionError("unexpected network")
    monkeypatch.setattr(reddit.requests, "get", unexpected)
    monkeypatch.setattr(reddit.requests, "post", unexpected)


def post(id="a", **kw):
    return dict(id=id, title="Hiring engineer", author="founder", subreddit="forhire",
                permalink=f"/r/forhire/comments/{id}/role/", created_utc=time.time(),
                selftext="Role description and apply information", is_self=True, **kw)


def page(*posts, after=None):
    return FakeResp(payload={"data": {"children": [{"data": p} for p in posts], "after": after}})


def test_public_mode_maps_posts_to_jobs(monkeypatch):
    calls = []
    def get(url, **kw):
        calls.append((url, kw))
        return page(post("a"), post("b"))
    monkeypatch.setattr(reddit.requests, "get", get)
    jobs = reddit.fetch({"reddit_subreddits": ["forhire", "hiring"]})
    assert [j.external_id for j in jobs] == ["a", "b"]
    for j in jobs:
        assert j.source == "reddit" and j.company == "u/founder"
        assert j.url.startswith("https://www.reddit.com/r/")
        assert datetime.fromisoformat(j.posted_at).tzinfo is not None
        assert "Subreddit: r/forhire" in j.snippet
        assert "Role description" in j.snippet
    assert calls[0][0].endswith("/r/forhire+hiring/new.json")
    assert calls[0][1]["headers"]["User-Agent"] == reddit.USER_AGENT


def test_skips_stickied_nsfw_removed(monkeypatch):
    monkeypatch.setattr(reddit.requests, "get", lambda *a, **k: page(
        post("a", stickied=True), post("b", over_18=True),
        post("c", removed_by_category="moderator"), post("d")))
    assert [j.external_id for j in reddit.fetch({})] == ["d"]


def test_does_not_filter_for_hire_posts(monkeypatch):
    p = post(); p["title"] = "[For Hire] React dev"
    monkeypatch.setattr(reddit.requests, "get", lambda *a, **k: page(p))
    assert reddit.fetch({})[0].title == p["title"]


def test_stops_at_age_cutoff(monkeypatch):
    old = post("old"); old["created_utc"] -= 72 * 3600
    calls = []
    def get(*a, **k):
        calls.append(k)
        return page(post(), old, post("later"), after="cursor")
    monkeypatch.setattr(reddit.requests, "get", get)
    assert [j.external_id for j in reddit.fetch({"max_age_hours": 48})] == ["a"]
    assert len(calls) == 1


def test_paginates_until_cap(monkeypatch):
    calls = []
    def get(*a, **kw):
        calls.append(kw)
        return page(post("a"), post("b"), after="t3_x") if len(calls) == 1 else page(post("c"), post("d"))
    monkeypatch.setattr(reddit.requests, "get", get)
    assert [j.external_id for j in reddit.fetch({"max_per_source": 3})] == ["a", "b", "c"]
    assert calls[1]["params"]["after"] == "t3_x"


def test_http_403_returns_empty_without_raising(monkeypatch):
    monkeypatch.setattr(reddit.requests, "get", lambda *a, **k: FakeResp(403, text="blocked"))
    assert reddit.fetch({}) == []


def test_oauth_mode_uses_token(monkeypatch):
    monkeypatch.setenv("REDDIT_CLIENT_ID", "client")
    monkeypatch.setenv("REDDIT_CLIENT_SECRET", "secret")
    def token(*a, **kw):
        assert kw["auth"] == ("client", "secret")
        assert kw["data"] == {"grant_type": "client_credentials"}
        return FakeResp(200, {"access_token": "tok"})
    def get(url, **kw):
        assert url.startswith(reddit.OAUTH_BASE) and url.endswith("/new")
        assert kw["headers"]["Authorization"] == "bearer tok"
        return page(post())
    monkeypatch.setattr(reddit.requests, "post", token)
    monkeypatch.setattr(reddit.requests, "get", get)
    assert reddit.fetch({})[0].external_id == "a"


def test_oauth_failure_falls_back_to_public(monkeypatch):
    monkeypatch.setenv("REDDIT_CLIENT_ID", "client")
    monkeypatch.setenv("REDDIT_CLIENT_SECRET", "secret")
    monkeypatch.setattr(reddit.requests, "post", lambda *a, **k: FakeResp(401))
    def get(url, **kw):
        assert url.startswith(reddit.PUBLIC_BASE) and url.endswith("new.json")
        assert "Authorization" not in kw["headers"]
        return page(post())
    monkeypatch.setattr(reddit.requests, "get", get)
    assert reddit.fetch({})[0].external_id == "a"


def test_linked_page_enrichment(monkeypatch):
    p = post(); p.update(is_self=False, url="https://jobs.example/role")
    social = post("social"); social.update(is_self=False, url="https://i.redd.it/image")
    calls = []
    def bodies(urls, **kw):
        calls.append(urls)
        assert kw == {"max_chars": 2500, "workers": 6}
        return {p["url"]: "Senior engineer, remote EU"}
    monkeypatch.setattr(_detail_fetch, "fetch_many_bodies", bodies)
    monkeypatch.setattr(reddit.requests, "get", lambda *a, **k: page(p, social))
    jobs = reddit.fetch({})
    assert calls == [[p["url"]]]
    assert "--- Linked page (" in jobs[0].snippet and "Senior engineer, remote EU" in jobs[0].snippet
    assert "Linked page" not in jobs[1].snippet


def test_cross_source_dedupe_keeps_distinct_authors(monkeypatch):
    a, b = post("a"), post("b"); b["author"] = "another"
    monkeypatch.setattr(reddit.requests, "get", lambda *a_, **k: page(a, b))
    assert {j.company for j in dedupe_cross_source(reddit.fetch({}))} == {"u/founder", "u/another"}


def test_telegram_exemptions():
    import telegram_client as tc
    assert {"reddit", "x_twitter"} <= tc._FORUM_SOURCE_EXEMPT
    assert {"reddit", "x_twitter"} <= tc._LIVENESS_EXEMPT_SOURCES
    url = "https://www.reddit.com/r/forhire/comments/abc/x/"
    assert tc._url_is_real_posting(url, "reddit")[0] is True
    assert tc._url_is_real_posting(url, "web_search")[0] is False


@pytest.mark.parametrize("enabled,expected", [(True, {"reddit", "x_twitter"}), (False, {"x_twitter"})])
def test_apify_backend_runs_local_only_sources(monkeypatch, enabled, expected):
    import search_jobs
    import apify_fetch
    monkeypatch.setattr(apify_fetch, "fetch_all_apify", lambda *a, **k: ([], [], []))
    for key in ("reddit", "x_twitter"):
        job = Job(key, key, "Role", key, "", "https://example.org", "")
        monkeypatch.setitem(search_jobs.SOURCES, key, SimpleNamespace(fetch=lambda f, j=job: [j]))
    jobs, errors = search_jobs._fetch_global({"sources": {"reddit": enabled, "x_twitter": True}}, backend="apify")
    assert {j.source for j in jobs} == expected
    assert errors == []


@pytest.mark.parametrize("host,external", [("old.reddit.com", False), ("v.redd.it", False),
    ("t.co", False), ("sub.twitter.com", False), ("notreddit.com", True), ("jobs.example", True)])
def test_external_host_boundaries(host, external):
    assert is_external_link(f"https://{host}/role") is external


def test_first_external_link_strips_punctuation():
    assert first_external_link("https://x.com/p then (https://jobs.example/role).,]") == "https://jobs.example/role"


@pytest.mark.parametrize("status", [401, 403, 429])
@pytest.mark.parametrize("host", ["www.reddit.com", "x.com", "mobile.twitter.com"])
def test_social_anti_bot_is_alive(monkeypatch, status, host):
    import telegram_client as tc
    monkeypatch.setattr(tc, "_head_with_429_retry", lambda *a: (FakeResp(status), None))
    assert tc._url_is_alive(f"https://{host}/post") == (True, f"social_anti_bot_{status}")


def test_partial_results_survive_network_failure(monkeypatch):
    calls = []
    def get(*a, **k):
        calls.append(1)
        if len(calls) > 1:
            raise reddit.requests.ConnectionError("offline")
        return page(post(), after="next")
    monkeypatch.setattr(reddit.requests, "get", get)
    assert [j.external_id for j in reddit.fetch({})] == ["a"]
