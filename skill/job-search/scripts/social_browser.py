#!/usr/bin/env python3
"""Authenticated loopback receiver for observed Reddit/X DOM posts.

This is a separate bounded cache, not the jobs database. Browser collection never
exports cookies, bypasses access controls, or decides whether a post is a job.
"""
from __future__ import annotations

import atexit
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import math
import os
from pathlib import Path
import re
import signal
import socket
import sqlite3
import threading
import time
from urllib.parse import urlsplit

import forensic
from dedupe import Job
from sources._social import enrich_with_linked_pages, first_external_link, is_external_link

log = logging.getLogger(__name__)
HOST = "127.0.0.1"
PORT = 8766
MAX_BODY = 1024 * 1024
MAX_BATCH = 100
MAX_ROWS = 5000
RETENTION_SECONDS = 7 * 86400
REQUEST_TIMEOUT = 5
FUTURE_TOLERANCE = 300
_FIELDS = {"id": 100, "title": 1000, "text": 50000, "author": 100,
           "display_name": 200, "url": 2048, "created_at": 100,
           "subreddit": 100, "external_url": 2048}


def _reject_json_constant(value):
    raise ValueError("non-finite JSON number")


def cache_path() -> Path:
    root = Path(__file__).resolve().parent.parent.parent.parent
    return root / os.environ.get("STATE_DIR", "state") / "social_browser.sqlite3"


@dataclass(frozen=True, repr=False)
class ReceiverConfig:
    token: str
    extension_id: str

    @property
    def origin(self) -> str:
        return f"chrome-extension://{self.extension_id}"


def receiver_config() -> ReceiverConfig | None:
    enabled = os.environ.get("OINK_SOCIAL_BROWSER_ENABLED", "").strip()
    if enabled in ("", "0"):
        return None
    if enabled != "1":
        raise ValueError("OINK_SOCIAL_BROWSER_ENABLED must be 0 or 1")
    token = os.environ.get("OINK_SOCIAL_BROWSER_TOKEN", "").strip()
    if not 32 <= len(token) <= 512 or not all(33 <= ord(c) <= 126 for c in token):
        raise ValueError("OINK_SOCIAL_BROWSER_TOKEN must be 32–512 printable non-space ASCII characters")
    extension_id = os.environ.get("OINK_SOCIAL_BROWSER_EXTENSION_ID", "").strip()
    if not re.fullmatch(r"[a-p]{32}", extension_id):
        raise ValueError("OINK_SOCIAL_BROWSER_EXTENSION_ID must be the exact 32-character Chrome extension ID")
    return ReceiverConfig(token, extension_id)


def collection_config() -> dict:
    from defaults import DEFAULTS

    import social_seeds

    enabled = DEFAULTS.get("sources", {})
    # Profile-generated seeds (union over searchable users), DEFAULTS fallback.
    db = None
    try:
        from db import DB
        db_path = cache_path().parent / "jobs.db"
        db = DB(db_path) if db_path.exists() else None
    except Exception as exc:
        log.warning("social_browser: jobs DB unavailable for seeds (%s)", type(exc).__name__)
    seeds = social_seeds.resolve(db, defaults=DEFAULTS)
    subreddits = list(seeds["reddit_subreddits"]) if enabled.get("reddit", True) else []
    queries = list(seeds["x_search_queries"]) if enabled.get("x_twitter", True) else []
    cap = DEFAULTS.get("max_per_source", 36)
    age = DEFAULTS.get("max_age_hours", 48)
    if (len(subreddits) > 50 or any(not isinstance(s, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,21}", s) for s in subreddits) or
            len(queries) > 12 or any(not isinstance(q, str) or not q.strip() or len(q) > 500 for q in queries) or
            type(cap) is not int or not 1 <= cap <= 100 or
            type(age) not in (int, float) or not math.isfinite(age) or not 0 < age <= 168):
        raise ValueError("social browser collection settings in DEFAULTS exceed protocol bounds")
    return {"reddit_subreddits": subreddits, "x_search_queries": queries,
            "max_per_source": cap, "max_age_hours": age}


def normalize_post(source: str, post, *, now: float | None = None) -> dict | None:
    """Accept only structurally valid observations; never synthesize dates/IDs."""
    if source not in ("reddit", "x_twitter") or not isinstance(post, dict):
        return None
    fields = {}
    for name, limit in _FIELDS.items():
        value = post.get(name, "")
        if not isinstance(value, str) or len(value) > limit or "\x00" in value:
            return None
        try:
            value.encode("utf-8")
        except UnicodeError:
            return None
        fields[name] = value.strip()
    ident, author, url = fields["id"], fields["author"], fields["url"]
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.username or parsed.password or
                parsed.port is not None or parsed.fragment or not parsed.hostname):
            return None
        dt = datetime.fromisoformat(fields["created_at"].replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return None
        dt = dt.astimezone(timezone.utc)
        created = dt.timestamp()
        now = time.time() if now is None else now
        if not math.isfinite(created) or created < now - RETENTION_SECONDS or created > now + FUTURE_TOLERANCE:
            return None
    except (ValueError, OverflowError):
        return None
    if source == "reddit":
        if (parsed.hostname not in ("reddit.com", "www.reddit.com", "old.reddit.com", "new.reddit.com") or
                not re.fullmatch(r"[a-z0-9]{1,20}", ident) or
                not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", author)):
            return None
        match = re.fullmatch(r"/r/([A-Za-z0-9_]{1,32})/comments/([a-z0-9]+)/?(?:([^/]+)/?)?", parsed.path)
        if not match or match[2] != ident or not fields["title"]:
            return None
        subreddit = match[1]
        if fields["subreddit"] and fields["subreddit"].lower() != subreddit.lower():
            return None
        fields["subreddit"] = subreddit
        # Retain the observed title slug for server-rendered desktop navigation.
        fields["url"] = f"https://www.reddit.com{parsed.path.rstrip('/')}/"
    else:
        if (parsed.hostname not in ("x.com", "www.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com") or
                not re.fullmatch(r"[0-9]{1,30}", ident) or
                not re.fullmatch(r"[A-Za-z0-9_]{1,15}", author) or not fields["text"]):
            return None
        match = re.fullmatch(r"/([A-Za-z0-9_]{1,15})/status/([0-9]+)/?", parsed.path)
        if not match or match[1].lower() != author.lower() or match[2] != ident:
            return None
        fields["url"] = f"https://x.com/{author}/status/{ident}"
    link = fields["external_url"]
    if link:
        try:
            target = urlsplit(link)
            if (target.scheme not in ("https", "http") or not target.hostname or
                    target.username or target.password or any(c.isspace() for c in link)):
                return None
            target.port  # Reject malformed port syntax, without fetching the URL.
        except ValueError:
            return None
    fields.update(source=source, created=created, created_at=dt.isoformat())
    return fields


class SocialCache:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path is not None else cache_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("""CREATE TABLE IF NOT EXISTS posts (
                source TEXT NOT NULL, id TEXT NOT NULL, created REAL NOT NULL,
                observed REAL NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY (source, id))""")
            db.execute("CREATE INDEX IF NOT EXISTS posts_created ON posts(created)")
            self._prune(db, time.time())

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _prune(db, now):
        db.execute("DELETE FROM posts WHERE created < ? OR observed < ?",
                   (now - RETENTION_SECONDS, now - RETENTION_SECONDS))
        db.execute("""DELETE FROM posts WHERE rowid IN (
            SELECT rowid FROM posts ORDER BY created DESC, observed DESC LIMIT -1 OFFSET ?)
            """, (MAX_ROWS,))

    def ingest(self, source: str, posts: list) -> int:
        now = time.time()
        normalized = [item for post in posts if (item := normalize_post(source, post, now=now)) is not None]
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for item in normalized:
                row = db.execute("SELECT payload FROM posts WHERE source=? AND id=?",
                                 (source, item["id"])).fetchone()
                if row:
                    previous = json.loads(row[0])
                    # Feed cards may be truncated after a full detail observation.
                    if len(previous["text"]) > len(item["text"]):
                        item["text"] = previous["text"]
                    for name in ("title", "external_url", "display_name"):
                        if not item[name]:
                            item[name] = previous[name]
                db.execute("""INSERT INTO posts(source,id,created,observed,payload) VALUES(?,?,?,?,?)
                    ON CONFLICT(source,id) DO UPDATE SET created=excluded.created,
                    observed=excluded.observed,payload=excluded.payload""",
                           (source, item["id"], item["created"], now, json.dumps(item, ensure_ascii=False)))
            self._prune(db, now)
        return len(normalized)

    def fetch(self, source: str, filters: dict, *, links: dict[str, str] | None = None) -> list[Job]:
        cap = max(0, min(MAX_ROWS, int(filters.get("max_per_source", 36))))
        age = max(0, min(168, float(filters.get("max_age_hours", 48))))
        if not math.isfinite(age):
            return []
        if cap == 0:
            return []
        now = time.time()
        with self._connect() as db:
            self._prune(db, now)
            rows = db.execute("""SELECT payload FROM posts WHERE source=? AND created>=?
                AND created<=? ORDER BY created DESC, id LIMIT ?""",
                              (source, now - age * 3600, now + FUTURE_TOLERANCE, MAX_ROWS)).fetchall()
        jobs = []
        subs = {s.lower() for s in filters.get("reddit_subreddits", [])}
        for row in rows:
            item = normalize_post(source, json.loads(row[0]), now=now)
            if item is None:
                continue
            if source == "reddit":
                if subs and item["subreddit"].lower() not in subs:
                    continue
                title = item["title"]
                snippet = f"Subreddit: r/{item['subreddit']}\nTitle: {title}\n{item['text']}"
                company = f"u/{item['author']}"
            else:
                title = item["title"] or item["text"].splitlines()[0]
                snippet = f"Author: {item['display_name']} (@{item['author']})\n{item['text']}"
                company = f"@{item['author']}"
            # Retain a separately observed application link even for long text.
            link = f"\nLink: {item['external_url']}" if item["external_url"] else ""
            jobs.append(Job(source, item["id"], title[:200], company, "", item["url"],
                            item["created_at"], snippet[:max(0, 4000 - len(link))] + link))
            if links is not None:
                # Use observed fields before snippet truncation, never a thread
                # permalink or an inferred destination. t.co is resolved only by
                # the shared detail transport's per-hop SSRF-safe redirects.
                target = item["external_url"]
                if not is_external_link(target, allow_shortener=True):
                    target = first_external_link(item["text"], allow_shortener=True)
                if target:
                    links[item["id"]] = target
            if len(jobs) >= cap:
                break
        return jobs


def fetch_cached(source: str, filters: dict) -> list[Job]:
    """Read observed posts, enriching only their external links (never APIs/actors)."""
    jobs = []
    links = {}
    error = None
    try:
        if cache_path().exists():
            jobs = SocialCache().fetch(source, filters, links=links)
    except Exception as exc:
        log.warning("%s: browser cache unavailable (%s)", source, type(exc).__name__)
        error = {"class": type(exc).__name__}
    jobs, enriched = enrich_with_linked_pages(jobs, links, source_label=source)
    forensic.log_step(f"{source}.fetch", output={
        "mode": "browser", "jobs": len(jobs), "linked_pages": len(links), "enriched": enriched,
    }, error=error)
    return jobs


class SocialBrowserServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, config: ReceiverConfig, cache: SocialCache):
        self.config = config
        self.cache = cache
        self._slots = threading.BoundedSemaphore(16)
        super().__init__(address, SocialBrowserHandler)

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(REQUEST_TIMEOUT)
        return connection, address

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def handle_error(self, request, client_address):
        log.warning("social_browser: request failed")


class SocialBrowserHandler(BaseHTTPRequestHandler):
    server: SocialBrowserServer
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):
        pass  # Never log request paths, credentials, or payloads.

    def _reply(self, status, body):
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if self.headers.get_all("Origin") == [self.server.config.origin]:
            self.send_header("Access-Control-Allow-Origin", self.server.config.origin)
            self.send_header("Vary", "Origin")
        if self.command == "OPTIONS" and status == 200:
            self.send_header("Access-Control-Allow-Methods", "GET, POST")
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
            self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()
        self.wfile.write(payload)
        self.close_connection = True

    def _boundary(self, *, preflight=False):
        if (self.client_address[0] != HOST or self.headers.get_all("Host") != [f"{HOST}:{PORT}"]):
            self._reply(403, {"error": "host rejected"})
            return False
        origins = self.headers.get_all("Origin")
        if origins is not None and origins != [self.server.config.origin]:
            self._reply(403, {"error": "origin rejected"})
            return False
        if preflight:
            if origins is None:
                self._reply(403, {"error": "origin required"})
                return False
        else:
            auth = self.headers.get_all("Authorization") or []
            expected = f"Bearer {self.server.config.token}".encode()
            if len(auth) != 1 or not hmac.compare_digest(auth[0].encode(), expected):
                self._reply(401, {"error": "authentication required"})
                return False
        if self.headers.get_all("Transfer-Encoding"):
            self._reply(400, {"error": "transfer encoding unsupported"})
            return False
        return True

    def do_OPTIONS(self):
        if not self._boundary(preflight=True):
            return
        headers = {h.strip().lower() for h in self.headers.get("Access-Control-Request-Headers", "").split(",") if h.strip()}
        if (self.path not in ("/v1/config", "/v1/posts") or
                self.headers.get("Access-Control-Request-Method") not in ("GET", "POST") or
                not headers <= {"authorization", "content-type"}):
            self._reply(403, {"error": "preflight rejected"})
            return
        self._reply(200, {})

    def do_GET(self):
        if not self._boundary():
            return
        lengths = self.headers.get_all("Content-Length")
        if lengths not in (None, ["0"]):
            self._reply(400, {"error": "unexpected body"})
        elif self.path == "/v1/config":
            self._reply(200, collection_config())
        else:
            self._reply(404, {"error": "not found"})

    def do_POST(self):
        if not self._boundary():
            return
        if self.path != "/v1/posts":
            self._reply(404, {"error": "not found"})
            return
        content_types = self.headers.get_all("Content-Type") or []
        if (len(content_types) != 1 or not re.fullmatch(
                r"application/json(?:\s*;\s*charset=utf-8)?", content_types[0], re.IGNORECASE)):
            self._reply(415, {"error": "application/json required"})
            return
        lengths = self.headers.get_all("Content-Length") or []
        if len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,10}", lengths[0]):
            self._reply(411, {"error": "content length required"})
            return
        size = int(lengths[0])
        if size > MAX_BODY:
            self._reply(413, {"error": "body too large"})
            return
        try:
            raw = self.rfile.read(size)
            if len(raw) != size:
                self._reply(400, {"error": "incomplete body"})
                return
            body = json.loads(raw.decode("utf-8"), parse_constant=_reject_json_constant)
        except (UnicodeError, ValueError, RecursionError):
            self._reply(400, {"error": "invalid JSON"})
            return
        except (TimeoutError, socket.timeout):
            self._reply(408, {"error": "body timeout"})
            return
        if (not isinstance(body, dict) or body.get("source") not in ("reddit", "x_twitter") or
                not isinstance(body.get("posts"), list) or len(body["posts"]) > MAX_BATCH):
            self._reply(400, {"error": "invalid batch"})
            return
        try:
            accepted = self.server.cache.ingest(body["source"], body["posts"])
        except (sqlite3.Error, OSError):
            log.warning("social_browser: cache write failed")
            self._reply(503, {"error": "cache unavailable"})
            return
        self._reply(200, {"accepted": accepted})


class RunningReceiver:
    def __init__(self, server: SocialBrowserServer):
        self.server = server
        self.thread = threading.Thread(target=server.serve_forever, name="social-browser", daemon=True)
        self.thread.start()
        self._closed = False

    def close(self):
        if not self._closed:
            self._closed = True
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=REQUEST_TIMEOUT + 1)

    shutdown = close


def start_social_browser_server(*, cache_path: Path | str | None = None) -> RunningReceiver | None:
    config = receiver_config()
    if config is None:
        return None
    collection_config()  # Fail visibly at startup rather than breaking extension refresh.
    cache = SocialCache(cache_path)
    server = SocialBrowserServer((HOST, PORT), config, cache)
    receiver = RunningReceiver(server)
    atexit.register(receiver.close)
    log.info("social_browser: listening on http://%s:%d", HOST, PORT)
    return receiver


def main() -> int:
    from search_jobs import load_env

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    load_env()
    try:
        receiver = start_social_browser_server()
    except (ValueError, OSError, sqlite3.Error) as exc:
        if isinstance(exc, ValueError):
            log.error("social_browser: %s", exc)
        else:
            log.error("social_browser: cannot start (%s); check cache permissions and port 8766", type(exc).__name__)
        return 1
    if receiver is None:
        log.error("social_browser: set OINK_SOCIAL_BROWSER_ENABLED=1 to start the receiver")
        return 1
    stopped = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopped.set())
    try:
        stopped.wait()
    finally:
        receiver.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
