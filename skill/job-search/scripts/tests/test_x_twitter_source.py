"""X API and explicit-opt-in Apify source behavior without live network."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import forensic
from sources import x_twitter as x, _detail_fetch


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
            raise x.requests.HTTPError(str(self.status_code))


def boom(*a, **k):
    raise AssertionError("unexpected network or actor spending")


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    for key in ("X_BEARER_TOKEN", "APIFY_TOKEN", "X_APIFY_ACTOR"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(forensic, "log_step", lambda *a, **k: None)
    monkeypatch.setattr(_detail_fetch, "fetch_many_bodies", lambda *a, **k: {})
    monkeypatch.setattr(x.requests, "get", boom)
    monkeypatch.setattr(x.requests, "post", boom)
    monkeypatch.setattr(x, "run_actor", boom)


def tweet(id="1", **kw):
    return dict(id=id, text="Hiring a developer\nApply here", author_id="u1",
                created_at=datetime.now(timezone.utc).isoformat(), **kw)


def payload(*tweets):
    return {"data": list(tweets), "includes": {"users": [{"id": "u1", "username": "founder", "name": "Founder"}]}}


def test_no_credentials_returns_empty_and_no_network():
    assert x.fetch({}) == []


def test_apify_token_without_actor_does_not_spend(monkeypatch):
    monkeypatch.setenv("APIFY_TOKEN", "token")
    assert x.fetch({}) == []


def test_api_mode_maps_tweets(monkeypatch):
    monkeypatch.setenv("X_BEARER_TOKEN", "bearer")
    monkeypatch.setenv("APIFY_TOKEN", "token")
    monkeypatch.setenv("X_APIFY_ACTOR", "must/not-run")
    note = tweet("2", note_tweet={"text": "Full long post with complete requirements"})
    def get(url, **kw):
        assert url == "https://api.x.com/2/tweets/search/recent"
        assert kw["headers"] == {"Authorization": "Bearer bearer"}
        assert 10 <= kw["params"]["max_results"] <= 100
        return FakeResp(payload=payload(tweet(), note))
    monkeypatch.setattr(x.requests, "get", get)
    jobs = x.fetch({"x_search_queries": ["query"]})
    assert [j.external_id for j in jobs] == ["1", "2"]
    assert jobs[0].url == "https://x.com/founder/status/1"
    assert all(j.source == "x_twitter" and j.company == "@founder" for j in jobs)
    assert "Full long post with complete requirements" in jobs[1].snippet
    assert all(datetime.fromisoformat(j.posted_at).tzinfo is not None for j in jobs)


def test_api_mode_dedupes_across_queries(monkeypatch):
    monkeypatch.setenv("X_BEARER_TOKEN", "bearer")
    calls = []
    def get(*a, **kw):
        calls.append(kw["params"]["query"])
        return FakeResp(payload=payload(tweet()))
    monkeypatch.setattr(x.requests, "get", get)
    assert [j.external_id for j in x.fetch({"x_search_queries": ["one", "two"]})] == ["1"]
    assert calls == ["one", "two"]


@pytest.mark.parametrize("response", [FakeResp(payload={"meta": {"result_count": 0}}), FakeResp(401)])
def test_api_mode_handles_empty_and_401(monkeypatch, response):
    monkeypatch.setenv("X_BEARER_TOKEN", "bearer")
    monkeypatch.setattr(x.requests, "get", lambda *a, **k: response)
    assert x.fetch({"x_search_queries": ["query"]}) == []


def test_apify_mode_maps_items(monkeypatch):
    monkeypatch.setenv("APIFY_TOKEN", "token")
    monkeypatch.setenv("X_APIFY_ACTOR", "some/actor")
    date = datetime.now(timezone.utc).strftime("%a %b %d %H:%M:%S +0000 %Y")
    def actor(slug, run_input, token, timeout):
        assert (slug, token, timeout) == ("some/actor", "token", 600)
        assert run_input == {"searchTerms": ["query"], "maxItems": 36, "sort": "Latest"}
        return [None, {"noResults": True}, {"id": "retweet", "isRetweet": True},
                {"id": "1", "fullText": "Hiring engineer", "createdAt": date,
                 "author": {"userName": "founder", "name": "Founder"}},
                {"id_str": "2", "full_text": "Hiring designer", "created_at": date,
                 "user": {"screen_name": "design"}, "twitterUrl": "https://twitter.com/design/status/2"}]
    monkeypatch.setattr(x, "run_actor", actor)
    jobs = x.fetch({"x_search_queries": ["query"]})
    assert [j.external_id for j in jobs] == ["1", "2"]
    assert jobs[0].company == "@founder" and jobs[0].url == "https://x.com/founder/status/1"
    assert jobs[1].url == "https://twitter.com/design/status/2"
    assert all(datetime.fromisoformat(j.posted_at).tzinfo is not None for j in jobs)


def test_parse_created_formats():
    iso = x._parse_created("2026-11-05T12:34:56.000Z")
    legacy = x._parse_created("Thu Nov 05 12:34:56 +0000 2026")
    assert iso == legacy == datetime(2026, 11, 5, 12, 34, 56, tzinfo=timezone.utc)
    assert x._parse_created("garbage") is None


def test_title_from_text():
    assert x._title_from_text("  Hiring   https://example.org/role  engineer\nDetails") == "Hiring engineer"
    assert x._title_from_text("a" * 110) == "a" * 100 + "…"


def test_apify_age_and_missing_dates(monkeypatch):
    monkeypatch.setenv("APIFY_TOKEN", "token")
    monkeypatch.setenv("X_APIFY_ACTOR", "some/actor")
    old = tweet("old"); old["created_at"] = (datetime.now(timezone.utc) - timedelta(hours=49)).isoformat()
    missing = tweet("missing"); missing["created_at"] = "bad-date"
    monkeypatch.setattr(x, "run_actor", lambda *a, **k: [old, missing, tweet("fresh")])
    assert [j.external_id for j in x.fetch({})] == ["fresh"]


def test_api_clamps_lookback_and_enriches_note_links(monkeypatch):
    monkeypatch.setenv("X_BEARER_TOKEN", "bearer")
    link = "https://jobs.example/role"
    t = tweet(note_tweet={"text": "Long posting", "entities": {"urls": [
        {"expanded_url": "https://t.co/short"}, {"expanded_url": link}]}})
    def get(*a, **kw):
        start = datetime.fromisoformat(kw["params"]["start_time"].replace("Z", "+00:00"))
        age = datetime.now(timezone.utc) - start
        assert timedelta(days=6, hours=23, minutes=58) < age < timedelta(days=7)
        return FakeResp(payload=payload(t))
    monkeypatch.setattr(x.requests, "get", get)
    monkeypatch.setattr(_detail_fetch, "fetch_many_bodies", lambda urls, **kw: {link: "Remote EU role"})
    job = x.fetch({"max_age_hours": 1000, "x_search_queries": ["q"]})[0]
    assert f"--- Linked page ({link}) ---\nRemote EU role" in job.snippet


@pytest.mark.parametrize("status,expected", [(403, ["1"]), (429, ["1"]), (500, ["1", "3"])])
def test_api_error_stops_or_continues_queries(monkeypatch, status, expected):
    monkeypatch.setenv("X_BEARER_TOKEN", "bearer")
    responses = iter([FakeResp(payload=payload(tweet())), FakeResp(status), FakeResp(payload=payload(tweet("3")))])
    monkeypatch.setattr(x.requests, "get", lambda *a, **k: next(responses))
    assert [j.external_id for j in x.fetch({"x_search_queries": ["a", "b", "c"]})] == expected


def test_enrichment_failure_preserves_post(monkeypatch):
    monkeypatch.setenv("X_BEARER_TOKEN", "bearer")
    t = tweet(entities={"urls": [{"expanded_url": "https://jobs.example/role"}]})
    monkeypatch.setattr(x.requests, "get", lambda *a, **k: FakeResp(payload=payload(t)))
    monkeypatch.setattr(_detail_fetch, "fetch_many_bodies", boom)
    assert [j.external_id for j in x.fetch({"x_search_queries": ["q"]})] == ["1"]
