# FindJobs Wiki — Log

Chronological append-only record of ingests, queries, and lint passes.

Format: `## [YYYY-MM-DD] <ingest|query|lint> | <short title>` + bullets.

## [2026-04-29] ingest | wiki bootstrap
- Scaffolded `claude-docs/` per global schema at `~/.claude/claude-docs/CLAUDE.md`.
- Created empty `raw/`, `wiki/`, `index.md`, `log.md`, project-scoped `CLAUDE.md`.
- Added pointer to root `CLAUDE.md`.

## [2026-11-06] ingest | Reddit + X sources
- Added global Reddit public/OAuth and X API/explicit-opt-in Apify sources, shared linked-page enrichment, AI scoring rule R9, source controls, and configuration documentation.
- Wired `_LOCAL_ONLY_SOURCES` into the apify backend alongside published actors so Reddit and X run on either fetch backend.

## [2026-11-08] ingest | Background Chrome social collection
- Added an opt-in Manifest V3 Reddit/X collector, authenticated extension-pinned loopback receiver, durable bounded cache, and cache-only source transport on both fetch backends. Browser sessions stay in Chrome; access challenges pause collection.
- Verified actual extension options, manual/alarm collection, challenge pause/resume and tab cleanup with controlled rendered pages; cache-to-pipeline smoke passed and 203 Python regression tests passed. Setup and operating limits are documented in README.

## [2026-11-08] ingest | Repair browser social posting content
- Reddit now follows missing-body posts through their full observed permalinks; preserving the title slug avoids empty inactive-tab SPA shells. Live Chrome collection recovered bodies of 2,762, 2,445, and 1,689 characters at the collection cap, with owned tabs cleaned up.
- Browser sources enrich external application pages, resolving X's HTTP/HTML `t.co` redirects through safe transport and reserving snippet space for linked content. Changed job content refreshes stored jobs and invalidates stale scores/queued verdicts while preserving identity, first-seen and delivery history.
- Verified 295 Python regression tests plus live Reddit collection and X external-page retrieval. Reload unpacked extension version 1.0.1 after updating; normal blocked or JS-only destinations can still yield incomplete content.

## [2026-11-11] query | Why Reddit/X yielded ~no matches → profile-driven targeting
- Diagnosis (state/jobs.db, Nov 8–11): 318 X + 223 Reddit posts scored; X max score 2, 71% scored 0; only 2 Reddit sends. Pipeline healthy — inputs were wrong: two generic global X queries (all disciplines, newest-first) dominated by aggregator bots (~40% of posts from ~5 accounts); 131/223 Reddit posts were "[For Hire]" self-promotion; API `-is:` operators were sent to x.com web search.
- Fix: profile builder rule 24c emits `search_seeds.social` (`x_queries` in X web syntax, `subreddits`); `social_seeds.resolve` unions searchable users' blocks (DEFAULTS fallback) for the extension's `/v1/config` and per-run `filters`; `to_api_query` translates `-filter:` → `-is:` for the X API path. Scorer R9 now separates direct hiring posts (silence on location/requirements is not a veto; up to 4 with DM-to-confirm note) from aggregator reposts (normal rules).
- Existing profiles need a rebuild to gain the block; until then DEFAULTS apply.

## [2026-11-11] change | Crypto / web3 sources added
- New global adapters (default ON, plain HTTP, no auth, in `_LOCAL_ONLY_SOURCES` so they run on the apify backend too): `web3_career` (listing HTML, `web3_career_pages`=3, ~15 rows/page), `cryptojobslist` (RSS at api.cryptojobslist.com/jobs.rss; no pubDate), `cryptocurrencyjobs` (Hugo RSS index.xml), `crypto_vc_boards` (Getro portfolio boards — 20 newest jobs in `__NEXT_DATA__` per `https://<board>/jobs`; Solana, Avalanche, Multicoin, Dragonfly, Electric Capital, CoinFund, Variant, Placeholder, Framework; `crypto_vc_boards_per_board`=12; ~55s sequential).
- Live yield 2026-11-11: 36 / 36 / 36 / 93. `max_total` 1200 → 1400.
- Not covered: web3.career official API (token-gated, docs.bondex.app); Consider.com boards (Paradigm, Pantera, a16z → now a16zfrontier) are POST-only; crypto.jobs + beincrypto 403; remote3 / useweb3 / jobs.sui.io are Supabase client-rendered; JobStash public JSON (`middleware.jobstash.xyz/jobs/list`) works but is now dominated by non-crypto listings (Databricks etc.), so skipped.
