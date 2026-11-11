"""Crypto / web3 source adapters: parsing, caps, failure handling, wiring."""
from pathlib import Path
import json
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import forensic
from sources import web3_career, cryptojobslist, cryptocurrencyjobs, crypto_vc_boards, degencryptojobs




class FakeResp:
    def __init__(self, status=200, text="", content=None):
        self.status_code = status
        self.text = text
        self.content = content if content is not None else text.encode()

    def raise_for_status(self):
        if self.status_code >= 400:
            raise web3_career.requests.HTTPError(str(self.status_code))


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(forensic, "log_step", lambda *a, **k: None)
    monkeypatch.setattr(web3_career, "PAGE_PAUSE_S", 0)
    monkeypatch.setattr(crypto_vc_boards, "BOARD_PAUSE_S", 0)

    def unexpected(*a, **k):
        raise AssertionError("unexpected network")
    # All adapters share the one `requests` module object.
    monkeypatch.setattr(web3_career.requests, "get", unexpected)


# ---------- web3.career ----------

def _w3_row(jid, title="Solidity Engineer", company="Acme DAO", loc="Remote", salary="$120k - $160k"):
    return f"""
<tr data-jobid={jid} class="job-row-grid table_row">
 <td class=cell-main><a href="/solidity-engineer-acme/{jid}"><h2> {title} </h2></a>
  <a href="/solidity-engineer-acme/{jid}"><h3> {company} </h3></a>
  <span class=job-location-mobile><span class=job-loc-pin>📍</span>{loc}</span></td>
 <td class=cell-posted><time datetime="2026-11-05 10:11:35+07:00">6d</time></td>
 <td class=cell-salary><p>{salary}</p></td>
 <td class=cell-tags><span><a href="/remote-jobs"> remote </a></span><span><a href="/solidity-jobs"> solidity </a></span></td>
</tr>"""


def _w3_page(*rows):
    return "<html><table>" + "".join(rows) + "<tr><td>footer</td></tr></table></html>"


def test_web3_career_parses_rows():
    jobs = web3_career.parse_listing(_w3_page(_w3_row(155983)))
    assert len(jobs) == 1
    j = jobs[0]
    assert j.source == "web3_career" and j.external_id == "155983"
    assert j.title == "Solidity Engineer" and j.company == "Acme DAO"
    assert j.location == "Remote"
    assert j.url == "https://web3.career/solidity-engineer-acme/155983"
    assert j.posted_at.startswith("2026-11-05")
    assert j.salary == "$120k - $160k"
    assert "solidity" in j.snippet


def test_web3_career_paginates_dedupes_and_caps(monkeypatch):
    pages = {
        "https://web3.career/": _w3_page(_w3_row(1), _w3_row(2)),
        "https://web3.career/?page=2": _w3_page(_w3_row(2), _w3_row(3)),  # 2 = featured repeat
        "https://web3.career/?page=3": _w3_page(_w3_row(4)),
    }
    calls = []
    def get(url, **kw):
        calls.append(url)
        return FakeResp(text=pages[url])
    monkeypatch.setattr(web3_career.requests, "get", get)
    jobs = web3_career.fetch({"max_per_source": 10, "web3_career_pages": 3})
    assert [j.external_id for j in jobs] == ["1", "2", "3", "4"]
    assert calls == list(pages)
    assert len(web3_career.fetch({"max_per_source": 2, "web3_career_pages": 3})) == 2


def test_web3_career_stops_on_error(monkeypatch):
    def get(url, **kw):
        return FakeResp(status=503, text="down")
    monkeypatch.setattr(web3_career.requests, "get", get)
    assert web3_career.fetch({"web3_career_pages": 3}) == []


# ---------- RSS boards ----------

CJL_RSS = b"""<?xml version="1.0"?>
<rss xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:media="http://search.yahoo.com/mrss/"><channel>
<item><title>Web3 Core Engineer</title>
<description>&lt;p&gt;Build wallet integrations in Rust.&lt;/p&gt;</description>
<link>https://cryptojobslist.com/jobs/web3-core-engineer-remote-at-brave</link>
<guid>https://cryptojobslist.com/jobs/web3-core-engineer-remote-at-brave</guid>
<dc:creator>Brave</dc:creator><media:location>Remote</media:location></item>
<item><title>Web3 Core Engineer</title>
<link>https://cryptojobslist.com/jobs/web3-core-engineer-remote-at-brave</link></item>
<item><title></title><link>https://cryptojobslist.com/jobs/empty</link></item>
</channel></rss>"""


def test_cryptojobslist_parses_and_dedupes():
    jobs = cryptojobslist.parse_feed(CJL_RSS)
    assert len(jobs) == 1
    j = jobs[0]
    assert (j.source, j.title, j.company, j.location) == ("cryptojobslist", "Web3 Core Engineer", "Brave", "Remote")
    assert j.external_id == j.url == "https://cryptojobslist.com/jobs/web3-core-engineer-remote-at-brave"
    assert "Rust" in j.snippet and "<p>" not in j.snippet


def test_cryptojobslist_fetch_handles_bad_xml(monkeypatch):
    monkeypatch.setattr(cryptojobslist.requests, "get", lambda url, **kw: FakeResp(content=b"Redirecting..."))
    assert cryptojobslist.fetch({}) == []


CCJ_RSS = b"""<?xml version="1.0"?><rss><channel>
<item><title>Bitcoin Mining Services - Business Development at Luxor Technology</title>
<description>Luxor Technology is looking to hire a Bitcoin Mining Services - Business Development to join their team. This is a full-time position that can be based in Miami, or San Francisco.</description>
<pubDate>Tue, 10 Nov 2026 23:29:51 +0200</pubDate>
<link>https://cryptocurrencyjobs.co/sales/luxor-technology-bitcoin-mining-services-business-development/</link></item>
<item><title>Head of Growth at Scale at Orbit Labs</title>
<description>Orbit Labs is looking to hire a Head of Growth at Scale. This is a full-time remote position.</description>
<link>https://cryptocurrencyjobs.co/marketing/orbit/</link></item>
</channel></rss>"""


def test_cryptocurrencyjobs_splits_title_and_location():
    a, b = cryptocurrencyjobs.parse_feed(CCJ_RSS)
    assert a.title == "Bitcoin Mining Services - Business Development"
    assert a.company == "Luxor Technology"
    assert a.location == "Miami, or San Francisco"
    assert a.posted_at.startswith("Tue, 10 Nov 2026")
    assert (b.title, b.company, b.location) == ("Head of Growth at Scale", "Orbit Labs", "Remote")


def test_cryptocurrencyjobs_caps(monkeypatch):
    monkeypatch.setattr(cryptocurrencyjobs.requests, "get", lambda url, **kw: FakeResp(content=CCJ_RSS))
    assert len(cryptocurrencyjobs.fetch({"max_per_source": 1})) == 1


# ---------- Getro crypto VC / ecosystem boards ----------

def _getro_job(jid, url, title="Protocol Engineer", company="Asymmetric Research"):
    return {
        "id": jid, "title": title, "url": url, "createdAt": 1794315202,
        "organization": {"name": company, "industryTags": ["Blockchain", "Web3"]},
        "locations": ["United States", "Europe"], "workMode": "remote",
        "seniority": "mid_senior", "skills": ["Rust", "Solidity"],
        "compensationAmountMinCents": 15000000, "compensationAmountMaxCents": 20000000,
        "compensationCurrency": "USD",
    }


def _getro_page(*jobs):
    data = {"props": {"pageProps": {"initialState": {"jobs": {"found": list(jobs), "total": 459}}}}}
    return f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(data)}</script></html>'


def test_getro_board_parses_next_data():
    (j,) = crypto_vc_boards.parse_board(
        _getro_page(_getro_job(99469556, "https://jobs.ashbyhq.com/asymmetric.re/395c")), "Solana ecosystem")
    assert (j.source, j.external_id, j.company) == ("crypto_vc_boards", "99469556", "Asymmetric Research")
    assert j.url == "https://jobs.ashbyhq.com/asymmetric.re/395c"
    assert j.location == "United States, Europe"
    assert j.salary == "150k–200k USD"
    assert j.posted_at.startswith("2026-11-")
    assert "Solana ecosystem" in j.snippet and "Rust" in j.snippet


def test_getro_board_without_next_data_is_empty():
    assert crypto_vc_boards.parse_board("<html>maintenance</html>", "x") == []


def test_getro_fetch_dedupes_across_boards_and_survives_failure(monkeypatch):
    shared = "https://jobs.lever.co/acme/1"
    pages = {
        "https://jobs.solana.com/jobs": FakeResp(text=_getro_page(_getro_job(1, shared), _getro_job(2, "https://a/2"))),
        "https://jobs.dragonfly.xyz/jobs": FakeResp(text=_getro_page(_getro_job(3, shared))),
        "https://jobs.broken.example/jobs": FakeResp(status=500),
    }
    monkeypatch.setattr(crypto_vc_boards.requests, "get", lambda url, **kw: pages[url])
    jobs = crypto_vc_boards.fetch({"crypto_vc_boards": [
        "jobs.solana.com", "jobs.dragonfly.xyz", "jobs.broken.example"]})
    assert [j.url for j in jobs] == [shared, "https://a/2"]


def test_getro_per_board_cap(monkeypatch):
    page = _getro_page(*[_getro_job(i, f"https://a/{i}") for i in range(20)])
    monkeypatch.setattr(crypto_vc_boards.requests, "get", lambda url, **kw: FakeResp(text=page))
    jobs = crypto_vc_boards.fetch({"crypto_vc_boards": ["jobs.solana.com"], "crypto_vc_boards_per_board": 5})
    assert len(jobs) == 5


# ---------- Degen Crypto Jobs ----------

def _degen_job(link, title="Smart Contract Engineer", date="2026-11-10T07:00:00Z", **kw):
    return {"title": title, "company": "Acme DAO", "location": ["Remote", "Portugal"],
            "salary": "$120K-$160K", "description": "<p>Build DeFi things.</p>",
            "tags": ["Engineering"], "topTags": ["Solidity", "DeFi"], "featured": False,
            "datePosted": date, "link": link, "source": "greenhouse", **kw}


def test_degencryptojobs_parses_sorts_and_dedupes():
    payload = {"jobs": [
        _degen_job("https://jobs.lever.co/x/old", title="Pinned Old", date="2026-08-20T10:00:00Z"),
        _degen_job("https://boards.greenhouse.io/x/1"),
        _degen_job("https://boards.greenhouse.io/x/1"),
        _degen_job("", title="No link"),
    ], "total": 4, "page": 1, "totalPages": 1}
    jobs = degencryptojobs.parse_jobs(payload)
    assert [j.url for j in jobs] == ["https://boards.greenhouse.io/x/1", "https://jobs.lever.co/x/old"]
    j = jobs[0]
    assert j.source == "degencryptojobs" and j.company == "Acme DAO"
    assert j.location == "Remote, Portugal" and j.salary == "$120K-$160K"
    assert "Solidity" in j.snippet and "Build DeFi things." in j.snippet and "<p>" not in j.snippet


def test_degencryptojobs_caps_and_survives_bad_payload(monkeypatch):
    class R(FakeResp):
        def __init__(self, data=None, bad=False):
            super().__init__(); self._d, self._bad = data, bad
        def json(self):
            if self._bad:
                raise ValueError("not json")
            return self._d
    payload = {"jobs": [_degen_job(f"https://x.test/{i}") for i in range(50)]}
    monkeypatch.setattr(degencryptojobs.requests, "get", lambda url, **kw: R(payload))
    assert len(degencryptojobs.fetch({"max_per_source": 10})) == 10
    monkeypatch.setattr(degencryptojobs.requests, "get", lambda url, **kw: R(bad=True))
    assert degencryptojobs.fetch({}) == []
    assert degencryptojobs.parse_jobs({"jobs": "nope"}) == []


# ---------- wiring ----------

def test_crypto_sources_are_registered_enabled_and_local():
    import search_jobs
    import bot
    from defaults import DEFAULTS
    catalog = {k for k, _ in bot._SOURCES_CATALOG}
    for key in ("web3_career", "cryptojobslist", "cryptocurrencyjobs", "crypto_vc_boards",
                "degencryptojobs"):
        assert key in search_jobs.SOURCES
        assert key in search_jobs._LOCAL_ONLY_SOURCES  # no Apify actor → runs on every backend
        assert DEFAULTS["sources"][key] is True
        assert key in catalog
