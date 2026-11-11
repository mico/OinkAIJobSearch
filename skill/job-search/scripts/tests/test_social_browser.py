"""Loopback ingestion, durable cache and browser-only social source integration."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import http.client
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import social_browser as sb
from sources import _detail_fetch, reddit, x_twitter
import safe_url

TOKEN = "local-test-token-" + "a" * 32
EXTENSION_ID = "a" * 32
ORIGIN = f"chrome-extension://{EXTENSION_ID}"


def post(source="reddit", ident=None, **changes):
    ident = ident or ("abc123" if source == "reddit" else "1234567890")
    value = {
        "id": ident, "title": "Hiring research engineer", "text": "Observed full post text and application details",
        "author": "founder", "display_name": "Founder", "subreddit": "forhire" if source == "reddit" else "",
        "url": f"https://www.reddit.com/r/forhire/comments/{ident}/hiring/" if source == "reddit" else f"https://x.com/founder/status/{ident}",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "external_url": "https://jobs.example.org/research",
    }
    value.update(changes)
    return value


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    monkeypatch.setenv("OINK_SOCIAL_BROWSER_ENABLED", "1")
    monkeypatch.setenv("OINK_SOCIAL_BROWSER_TOKEN", TOKEN)
    monkeypatch.setenv("OINK_SOCIAL_BROWSER_EXTENSION_ID", EXTENSION_ID)
    for key in ("REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET", "X_BEARER_TOKEN", "APIFY_TOKEN", "X_APIFY_ACTOR"):
        monkeypatch.delenv(key, raising=False)
    # Tests never contact linked sites unless a case supplies its own fake body.
    monkeypatch.setattr(_detail_fetch, "fetch_many_bodies", lambda *a, **kw: {})
    monkeypatch.setattr(safe_url, "safe_request", lambda *a, **kw: pytest.fail("unexpected short-link network request"))


@pytest.fixture
def receiver(tmp_path):
    running = []

    def start():
        cache = sb.SocialCache(tmp_path / "social_browser.sqlite3")
        server = sb.SocialBrowserServer((sb.HOST, 0), sb.receiver_config(), cache)
        handle = sb.RunningReceiver(server)
        running.append(handle)
        return handle

    yield start
    for handle in running:
        handle.close()


def request(handle, method="GET", path="/v1/config", *, payload=None, body=None,
            token=TOKEN, origin=ORIGIN, host="127.0.0.1:8766", extra=()):
    if payload is not None:
        body = json.dumps(payload).encode()
    connection = http.client.HTTPConnection("127.0.0.1", handle.server.server_port, timeout=3)
    connection.putrequest(method, path, skip_host=True)
    connection.putheader("Host", host)
    if token is not None:
        connection.putheader("Authorization", f"Bearer {token}")
    if origin is not None:
        connection.putheader("Origin", origin)
    if body is not None:
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", str(len(body)))
    for name, value in extra:
        connection.putheader(name, value)
    connection.endheaders(body)
    response = connection.getresponse()
    status, headers, result = response.status, dict(response.getheaders()), json.loads(response.read())
    connection.close()
    return status, headers, result


def ingest(handle, source="reddit", posts=None):
    return request(handle, "POST", "/v1/posts", payload={"source": source, "posts": posts if posts is not None else [post(source)]})


@pytest.mark.parametrize("token,origin,host,status", [
    (None, ORIGIN, "127.0.0.1:8766", 401),
    ("wrong", ORIGIN, "127.0.0.1:8766", 401),
    (TOKEN, "https://evil.example", "127.0.0.1:8766", 403),
    (TOKEN, "null", "127.0.0.1:8766", 403),
    (TOKEN, "chrome-extension://" + "b" * 32, "127.0.0.1:8766", 403),
    (TOKEN, ORIGIN, "evil.example:8766", 403),
    (TOKEN, ORIGIN, "localhost:8766", 403),
    (TOKEN, ORIGIN, "127.0.0.1", 403),
    (TOKEN, None, "127.0.0.1:8766", 200),
    (TOKEN, ORIGIN, "127.0.0.1:8766", 200),
])
def test_auth_origin_host(receiver, token, origin, host, status):
    actual, headers, _ = request(receiver(), token=token, origin=origin, host=host)
    assert actual == status
    assert headers.get("Access-Control-Allow-Origin") != "*"
    if origin is None:
        assert "Access-Control-Allow-Origin" not in headers


@pytest.mark.parametrize("header,value", [("Host", "127.0.0.1:8766"), ("Origin", ORIGIN), ("Authorization", f"Bearer {TOKEN}")])
def test_duplicate_authority_headers_rejected(receiver, header, value):
    assert request(receiver(), extra=[(header, value)])[0] in (401, 403)


def test_post_authentication_and_preflight(receiver):
    handle = receiver()
    assert request(handle, "POST", "/v1/posts", payload={"source": "reddit", "posts": []}, token=None)[0] == 401
    status, headers, _ = request(handle, "OPTIONS", "/v1/posts", token=None, extra=[
        ("Access-Control-Request-Method", "POST"),
        ("Access-Control-Request-Headers", "authorization,content-type")])
    assert status == 200
    assert headers["Access-Control-Allow-Origin"] == ORIGIN
    assert request(handle, "OPTIONS", token=None, origin="https://evil.example")[0] == 403
    assert request(handle, "OPTIONS", token=None, origin=None)[0] == 403


@pytest.mark.parametrize("method,path,body,extra,status", [
    ("GET", "/elsewhere", None, [], 404),
    ("GET", "/v1/config", b"{}", [], 400),
    ("POST", "/v1/posts", None, [], 415),
    ("POST", "/v1/posts", None, [("Content-Type", "application/json")], 411),
    ("POST", "/v1/posts", None, [("Content-Type", "text/plain"), ("Content-Length", "0")], 415),
    ("POST", "/v1/posts", None, [("Content-Type", "application/json"), ("Content-Length", "-1")], 411),
    ("POST", "/v1/posts", None, [("Content-Type", "application/json"), ("Content-Length", str(sb.MAX_BODY + 1))], 413),
    ("POST", "/v1/posts", b"{}", [("Content-Length", "2")], 411),
    ("POST", "/v1/posts", b"{}", [("Transfer-Encoding", "chunked")], 400),
    ("POST", "/v1/posts", b"not json", [], 400),
    ("POST", "/v1/posts", b"\xff", [], 400),
    ("POST", "/v1/posts", b"NaN", [], 400),
    ("POST", "/v1/posts", b"[]", [], 400),
    ("POST", "/v1/posts", b'{"source":"other","posts":[]}', [], 400),
])
def test_request_boundaries(receiver, method, path, body, extra, status):
    assert request(receiver(), method, path, body=body, extra=extra)[0] == status


def test_batch_limit_and_body_timeout(receiver, monkeypatch):
    handle = receiver()
    assert ingest(handle, posts=[post()] * 101)[0] == 400
    monkeypatch.setattr(sb, "REQUEST_TIMEOUT", 0.1)
    assert request(handle, "POST", "/v1/posts", extra=[
        ("Content-Type", "application/json"), ("Content-Length", "100")])[0] == 408


@pytest.mark.parametrize("source", ["reddit", "x_twitter"])
def test_ingest_restart_and_jobs(receiver, source):
    handle = receiver()
    status, _, body = ingest(handle, source)
    assert (status, body) == (200, {"accepted": 1})
    handle.close()
    restarted = receiver()
    jobs = (reddit if source == "reddit" else x_twitter).fetch({})
    assert len(jobs) == 1
    job = jobs[0]
    assert job.source == source
    assert job.company == ("u/founder" if source == "reddit" else "@founder")
    assert job.posted_at and "Observed full post text" in job.snippet
    assert "Link: https://jobs.example.org/research" in job.snippet
    assert restarted.server.cache.fetch(source, {})[0].job_id == job.job_id


@pytest.mark.parametrize("source,changes", [
    ("reddit", {"url": "https://jobs.example.org/apply"}),
    ("reddit", {"url": "https://www.reddit.com.evil.example/r/forhire/comments/abc123/"}),
    ("reddit", {"url": "https://www.reddit.com/r/forhire/comments/different/"}),
    ("reddit", {"url": "https://www.reddit.com/r/forhire/comments/abc123/title/comment/"}),
    ("reddit", {"subreddit": "other"}),
    ("reddit", {"id": "t3_abc123"}),
    ("reddit", {"title": ""}),
    ("x_twitter", {"url": "https://x.com/another/status/1234567890"}),
    ("x_twitter", {"url": "https://x.com/founder/status/9999999"}),
    ("x_twitter", {"author": "@founder"}),
    ("x_twitter", {"text": ""}),
    ("reddit", {"created_at": "2026-01-01T00:00:00"}),
    ("reddit", {"created_at": "not a date"}),
    ("reddit", {"created_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()}),
    ("reddit", {"created_at": (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()}),
    ("reddit", {"external_url": "javascript:alert(1)"}),
    ("reddit", {"external_url": "https://user:secret@jobs.example/role"}),
    ("reddit", {"text": "x" * 50001}),
    ("reddit", {"text": "\ud800"}),
    ("reddit", {"author": None}),
])
def test_malformed_observations_preserve_valid_cache(receiver, source, changes):
    handle = receiver()
    assert ingest(handle, source)[2] == {"accepted": 1}
    assert ingest(handle, source, [post(source, **changes), None, 42])[2] == {"accepted": 0}
    jobs = handle.server.cache.fetch(source, {})
    assert len(jobs) == 1
    assert "Observed full post text" in jobs[0].snippet


def test_mixed_batch_and_canonical_dates(receiver):
    handle = receiver()
    item = post(url="https://old.reddit.com/r/forhire/comments/abc123/title/?utm_source=feed",
                created_at=datetime.now(timezone(timedelta(hours=3))).isoformat())
    assert ingest(handle, posts=[{}, item])[2] == {"accepted": 1}
    job = handle.server.cache.fetch("reddit", {})[0]
    assert job.url == "https://www.reddit.com/r/forhire/comments/abc123/title/"
    assert job.posted_at.endswith("+00:00")
    normalized = sb.normalize_post("x_twitter", post("x_twitter", url="https://twitter.com/founder/status/1234567890?s=20"))
    assert normalized["url"] == "https://x.com/founder/status/1234567890"


def test_reobservation_preserves_full_text_and_link(receiver):
    handle = receiver()
    full = "Full observed details. " * 500
    assert ingest(handle, posts=[post(text=full)])[2] == {"accepted": 1}
    assert ingest(handle, posts=[post(text="Short card", external_url="")])[2] == {"accepted": 1}
    job = handle.server.cache.fetch("reddit", {})[0]
    assert "Full observed details." in job.snippet
    assert "Short card" not in job.snippet
    assert job.snippet.endswith("Link: https://jobs.example.org/research")
    assert len(job.snippet) <= 4000


def test_age_cap_subreddit_and_expiration(receiver, monkeypatch):
    handle = receiver()
    cache = handle.server.cache
    old = post(ident="old123", created_at=(datetime.now(timezone.utc) - timedelta(days=3)).isoformat())
    assert ingest(handle, posts=[post(), old])[2] == {"accepted": 2}
    assert len(cache.fetch("reddit", {})) == 1
    assert len(cache.fetch("reddit", {"max_age_hours": 168})) == 2
    assert cache.fetch("reddit", {"max_per_source": 0}) == []
    assert cache.fetch("reddit", {"reddit_subreddits": ["unrelated"]}) == []
    with cache._connect() as db:
        db.execute("UPDATE posts SET created=0 WHERE id='old123'")
    cache.fetch("reddit", {"max_age_hours": 168})
    with cache._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 1
    monkeypatch.setattr(sb, "MAX_ROWS", 2)
    assert cache.ingest("reddit", [post(ident="new1"), post(ident="new2"), post(ident="new3")]) == 3
    with cache._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 2


def test_concurrent_atomic_upserts(receiver):
    cache = receiver().server.cache
    variants = [post(text="Observed detail. " * count) for count in range(1, 9)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert list(pool.map(lambda item: cache.ingest("reddit", [item]), variants)) == [1] * 8
    jobs = cache.fetch("reddit", {})
    assert len(jobs) == 1
    assert ("Observed detail. " * 8).strip() in jobs[0].snippet


@pytest.mark.parametrize("source", ["reddit", "x_twitter"])
def test_browser_mode_never_calls_api_or_actor(monkeypatch, source):
    calls = []
    def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError("browser mode attempted an API or actor transport")
    monkeypatch.setattr(reddit.requests, "get", forbidden)
    monkeypatch.setattr(reddit.requests, "post", forbidden)
    monkeypatch.setattr(x_twitter, "run_actor", forbidden)
    linked_calls = []
    def linked_bodies(urls, **kwargs):
        linked_calls.append(urls)
        return {url: "Linked job requirements" for url in urls}
    monkeypatch.setattr(_detail_fetch, "fetch_many_bodies", linked_bodies)
    for key in ("REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET", "X_BEARER_TOKEN", "APIFY_TOKEN", "X_APIFY_ACTOR"):
        monkeypatch.setenv(key, "configured-but-must-not-be-used")
    adapter = reddit if source == "reddit" else x_twitter
    assert adapter.fetch({}) == []
    cache = sb.SocialCache()
    cache.ingest(source, [post(source)])
    jobs = adapter.fetch({})
    assert len(jobs) == 1
    assert "Linked job requirements" in jobs[0].snippet
    monkeypatch.setattr(sb, "cache_path", lambda: Path("/unreachable/no-cache.sqlite3"))
    assert adapter.fetch({}) == []
    assert calls == []
    assert linked_calls == [["https://jobs.example.org/research"]]


@pytest.mark.parametrize("source", ["reddit", "x_twitter"])
@pytest.mark.parametrize("link_field", ["external_url", "text"])
def test_browser_linked_body_reaches_consumer(monkeypatch, source, link_field):
    link = "https://jobs.example.org/full-description"
    # A raw-text URL outside the cached snippet must still be discovered.
    text = "Observed role context. " * 250
    item = post(source, text=text, external_url="")
    if link_field == "text":
        item["text"] += f"\nApply: {link}"
    else:
        item["external_url"] = link
    sb.SocialCache().ingest(source, [item])
    calls = []
    def linked_bodies(urls, **kwargs):
        calls.append((urls, kwargs))
        return {link: "Linked job body: salary EUR 95000; fully remote in EU."}
    monkeypatch.setattr(_detail_fetch, "fetch_many_bodies", linked_bodies)
    adapter = reddit if source == "reddit" else x_twitter
    jobs = adapter.fetch({})
    assert len(jobs) == 1
    assert jobs[0].source == source and jobs[0].external_id == item["id"]
    assert jobs[0].url == sb.normalize_post(source, item)["url"]
    assert "Observed role context." in jobs[0].snippet
    assert f"--- Linked page ({link}) ---" in jobs[0].snippet
    assert "Linked job body: salary EUR 95000; fully remote in EU." in jobs[0].snippet
    assert len(jobs[0].snippet) <= 4000
    assert calls == [([link], {"max_chars": 2500, "workers": 6})]


@pytest.mark.parametrize("link_field", ["external_url", "text"])
@pytest.mark.parametrize("redirect_kind", ["http", "html"])
def test_browser_shortener_uses_shared_safe_detail_transport(monkeypatch, link_field, redirect_kind):
    link = "https://t.co/observed-short-link"
    item = post("x_twitter", external_url="")
    if link_field == "text":
        item["text"] += f"\n{link}"
    else:
        item["external_url"] = link
    sb.SocialCache().ingest("x_twitter", [item])
    destination = "https://jobs.example.org/role?one=1&two=2"
    def resolve(method, url, **kwargs):
        assert method == "GET" and url == link
        assert kwargs["max_redirects"] == 0
        return SimpleNamespace(
            status_code=302 if redirect_kind == "http" else 200,
            headers={"Location": destination} if redirect_kind == "http" else {},
            text='<html><head><meta http-equiv="refresh" content="0;URL=https://jobs.example.org/role?one=1&amp;two=2"></head><body>Short link</body></html>',
            close=lambda: None,
        )
    monkeypatch.setattr(safe_url, "safe_request", resolve)
    calls = []
    def linked_bodies(urls, **kwargs):
        calls.extend(urls)
        return {destination: "Resolved external job description"}
    monkeypatch.setattr(_detail_fetch, "fetch_many_bodies", linked_bodies)
    jobs = x_twitter.fetch({})
    assert calls == [destination]
    assert "Resolved external job description" in jobs[0].snippet
    assert f"--- Linked page ({link}) ---" in jobs[0].snippet


@pytest.mark.parametrize("source", ["reddit", "x_twitter"])
@pytest.mark.parametrize("host", [
    "reddit.com", "old.reddit.com", "redd.it", "v.redd.it",
    "x.com", "mobile.x.com", "twitter.com", "mobile.twitter.com", "sub.t.co",
])
def test_browser_does_not_fetch_social_thread_links(monkeypatch, source, host):
    link = f"https://{host}/some-thread"
    sb.SocialCache().ingest(source, [post(source, external_url=link, text=f"Observed post {link}")])
    calls = []
    monkeypatch.setattr(_detail_fetch, "fetch_many_bodies", lambda *a, **kw: calls.append(a) or {})
    adapter = reddit if source == "reddit" else x_twitter
    jobs = adapter.fetch({})
    assert len(jobs) == 1 and "Observed post" in jobs[0].snippet
    assert "Linked page" not in jobs[0].snippet
    assert calls == []


@pytest.mark.parametrize("source", ["reddit", "x_twitter"])
def test_browser_observed_external_field_takes_priority(monkeypatch, source):
    link = "https://jobs.example.org/observed-application"
    sb.SocialCache().ingest(source, [post(
        source, external_url=link, text="Role details https://other.example.org/context",
    )])
    calls = []
    def linked_bodies(urls, **kwargs):
        calls.extend(urls)
        return {link: "Application requirements"}
    monkeypatch.setattr(_detail_fetch, "fetch_many_bodies", linked_bodies)
    events = []
    monkeypatch.setattr(sb.forensic, "log_step", lambda step, **kwargs: events.append((step, kwargs)))
    adapter = reddit if source == "reddit" else x_twitter
    assert "Application requirements" in adapter.fetch({})[0].snippet
    assert calls == [link]
    assert events == [(f"{source}.fetch", {
        "output": {"mode": "browser", "jobs": 1, "linked_pages": 1, "enriched": 1},
        "error": None,
    })]


@pytest.mark.parametrize("source", ["reddit", "x_twitter"])
def test_browser_social_payload_link_falls_back_to_external_text_link(monkeypatch, source):
    link = "https://jobs.example.org/from-text"
    sb.SocialCache().ingest(source, [post(
        source, external_url="https://www.reddit.com/r/forhire/comments/other/",
        text=f"Observed details https://x.com/founder/status/987 then {link}",
    )])
    calls = []
    def linked_bodies(urls, **kwargs):
        calls.extend(urls)
        return {link: "External role details"}
    monkeypatch.setattr(_detail_fetch, "fetch_many_bodies", linked_bodies)
    adapter = reddit if source == "reddit" else x_twitter
    assert "External role details" in adapter.fetch({})[0].snippet
    assert calls == [link]


@pytest.mark.parametrize("source", ["reddit", "x_twitter"])
@pytest.mark.parametrize("failure", ["empty", "missing", "exception"])
def test_browser_link_failure_preserves_observed_posts(monkeypatch, source, failure):
    cache = sb.SocialCache()
    cache.ingest(source, [post(source)])
    original = cache.fetch(source, {})
    def failed_bodies(urls, **kwargs):
        if failure == "exception":
            raise RuntimeError("linked page unavailable")
        return {url: "" for url in urls} if failure == "empty" else {}
    monkeypatch.setattr(_detail_fetch, "fetch_many_bodies", failed_bodies)
    adapter = reddit if source == "reddit" else x_twitter
    assert adapter.fetch({}) == original


@pytest.mark.parametrize("source", ["reddit", "x_twitter"])
def test_browser_long_post_retains_linked_body_and_provenance(monkeypatch, source):
    link = "https://jobs.example.org/" + "a" * 2000
    item = post(source, text="Original role context. " * 1500, external_url=link)
    sb.SocialCache().ingest(source, [item])
    body = "Linked requirements: five years Python, remote EU. " * 100
    monkeypatch.setattr(_detail_fetch, "fetch_many_bodies", lambda *a, **kw: {link: body})
    adapter = reddit if source == "reddit" else x_twitter
    job = adapter.fetch({})[0]
    assert "Original role context." in job.snippet
    assert job.snippet.startswith("Subreddit:" if source == "reddit" else "Author:")
    assert f"--- Linked page ({link}) ---" in job.snippet
    assert "Linked requirements: five years Python, remote EU." in job.snippet
    assert len(job.snippet) <= 4000


@pytest.mark.parametrize("backend", ["local", "apify"])
def test_existing_global_dispatch_consumes_browser_jobs(receiver, monkeypatch, backend):
    import search_jobs
    import apify_fetch
    handle = receiver()
    ingest(handle, "reddit")
    ingest(handle, "x_twitter")
    monkeypatch.setattr(apify_fetch, "fetch_all_apify", lambda *a, **kw: ([], [], []))
    enabled = {key: key in ("reddit", "x_twitter") for key in search_jobs.SOURCES}
    # remote_boards is dispatched by its three child toggles, not its registry key.
    enabled.update(remoteok=False, remotive=False, weworkremotely=False)
    def no_transport(*args, **kwargs):
        raise AssertionError("browser-cache dispatch must not contact remote transports")
    monkeypatch.setattr(reddit.requests, "get", no_transport)
    monkeypatch.setattr(reddit.requests, "post", no_transport)
    jobs, errors = search_jobs._fetch_global({"sources": enabled}, backend=backend)
    assert {job.source for job in jobs} == {"reddit", "x_twitter"}
    assert errors == []


def test_enable_configuration_and_clean_shutdown(receiver, monkeypatch):
    monkeypatch.setenv("OINK_SOCIAL_BROWSER_ENABLED", "0")
    assert sb.start_social_browser_server() is None
    monkeypatch.setenv("OINK_SOCIAL_BROWSER_ENABLED", "yes")
    with pytest.raises(ValueError, match="ENABLED"):
        sb.receiver_config()
    monkeypatch.setenv("OINK_SOCIAL_BROWSER_ENABLED", "1")
    monkeypatch.setenv("OINK_SOCIAL_BROWSER_TOKEN", "short")
    with pytest.raises(ValueError, match="TOKEN"):
        sb.receiver_config()
    monkeypatch.setenv("OINK_SOCIAL_BROWSER_TOKEN", TOKEN)
    monkeypatch.setenv("OINK_SOCIAL_BROWSER_EXTENSION_ID", "z" * 32)
    with pytest.raises(ValueError, match="EXTENSION_ID"):
        sb.receiver_config()
    monkeypatch.setenv("OINK_SOCIAL_BROWSER_EXTENSION_ID", EXTENSION_ID)
    handle = receiver()
    handle.close()
    handle.close()
    assert not handle.thread.is_alive()


def test_config_honors_global_source_switches(receiver, monkeypatch):
    from defaults import DEFAULTS
    monkeypatch.setitem(DEFAULTS["sources"], "reddit", False)
    status, _, config = request(receiver())
    assert status == 200
    assert config["reddit_subreddits"] == []
    assert config["x_search_queries"] == DEFAULTS["x_search_queries"]
    assert config["max_per_source"] == 36
    assert config["max_age_hours"] == 48


def test_authenticated_cli_post_without_origin(receiver):
    handle = receiver()
    status, headers, result = request(handle, "POST", "/v1/posts", origin=None,
                                      payload={"source": "reddit", "posts": [post()]})
    assert status == 200 and result == {"accepted": 1}
    assert "Access-Control-Allow-Origin" not in headers


def test_corrupt_cache_does_not_escape_source_fetch(monkeypatch):
    calls = []
    def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError("unexpected network")
    monkeypatch.setattr(reddit.requests, "get", forbidden)
    monkeypatch.setattr(reddit.requests, "post", forbidden)
    monkeypatch.setattr(x_twitter, "run_actor", forbidden)
    path = sb.cache_path()
    path.write_bytes(b"not a SQLite database")
    assert reddit.fetch({}) == []
    assert x_twitter.fetch({}) == []
    assert calls == []


def test_invalid_collection_bounds_fail_at_startup(monkeypatch):
    from defaults import DEFAULTS
    monkeypatch.setitem(DEFAULTS, "max_per_source", 101)
    with pytest.raises(ValueError, match="protocol bounds"):
        sb.start_social_browser_server()


@pytest.mark.parametrize("destination", [
    "https://x.com/another/status/123",
    "https://www.reddit.com/r/forhire/comments/other/",
    "https://t.co/original",
    "javascript:alert(1)",
])
def test_shortener_does_not_enrich_forums_or_redirect_loops(monkeypatch, destination):
    original = post("x_twitter", external_url="https://t.co/original")
    cache = sb.SocialCache()
    cache.ingest("x_twitter", [original])
    expected = cache.fetch("x_twitter", {})
    monkeypatch.setattr(safe_url, "safe_request", lambda *a, **kw: SimpleNamespace(
        status_code=302, headers={"Location": destination}, text="", close=lambda: None,
    ))
    monkeypatch.setattr(_detail_fetch, "fetch_many_bodies",
                        lambda *a, **kw: pytest.fail("no external job page resolved"))
    assert x_twitter.fetch({}) == expected
