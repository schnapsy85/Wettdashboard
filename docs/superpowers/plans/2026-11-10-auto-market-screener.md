# Auto Market Screener Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a two-profile car market screener that discovers qualifying public listings, refreshes them four times per day, and persists cross-device favorites in the dashboard.

**Architecture:** Keep the existing Python stdlib HTTP server and SQLite storage. A focused `auto_market.py` module owns normalized listings, filtering, persistence, and refresh orchestration; the HTML page consumes narrow JSON endpoints. Discovery uses a configured search provider plus an explicit source registry and source parsers, so broad discovery cannot bypass the data-quality rules.

**Tech Stack:** Python 3 stdlib, `sqlite3`, `urllib`, `html.parser`, existing `http.server` dashboard, plain HTML/CSS/JavaScript, systemd service/timer.

**Spec:** `docs/superpowers/specs/2026-11-10-auto-market-screener-design.md`

## Global Constraints

- Search both `VW Bus Multivan` long version and `Ford Tourneo Custom` long version together on one page.
- Both profiles require model-specific long version, registration year from 2025 onward, maximum 40,000 km, maximum 40,000 EUR, explicit accident-free evidence, and maximum 250 km from Norderstedt.
- Maximum price and maximum distance are editable filters pre-filled with 40,000 EUR and 250 km.
- Only public, trusted sources with enough structured data enter active results.
- Refresh four times per day in `Europe/Berlin` local time.
- Remove a listing only after confirmed unavailability; transient source failures must not delete it.
- Use the existing backend, SQLite, and plain HTML/CSS; add no frontend framework or dependency.
- Do not scrape login-only or paywalled pages, infer accident-free status, or guarantee complete web coverage.

## Review Focus

- A listing with a model name but no explicit long-version evidence must be rejected; test in Task 1.
- A listing with missing price, mileage, year, location, image, or canonical URL must not appear in the preview; test in Task 1.
- German price, mileage, decimal, and location formats must normalize without accepting invalid values; test in Task 1.
- A transient fetch/search failure must preserve an existing active listing, while a confirmed 404 removes it; test in Task 3.
- A favorite must survive a new browser session and be isolated behind the dashboard user's protected session; test in Task 2.

### Task 1: Listing domain, normalization, filtering, and database

**Files:**
- Create: `auto_market.py`
- Create: `tests/test_auto_market.py`

**Interfaces:**
- Produces `SEARCH_PROFILES`, `normalize_candidate(candidate: dict) -> dict | None`, `matches_filters(listing: dict, max_price_eur: int, max_distance_km: int) -> bool`, `auto_connect() -> sqlite3.Connection`, `list_active_listings(max_price_eur: int, max_distance_km: int) -> list[dict]`, `set_favorite(listing_id: int, favorite: bool) -> dict`, and `refresh_listings(discover, fetch) -> dict`.
- `normalize_candidate` accepts a provider candidate containing `profile`, `title`, `price_eur`, `mileage_km`, `year`, `location`, `distance_km`, `image_url`, `url`, `source`, `long_version_evidence`, and `accident_free_evidence`.

- [ ] **Step 1: Write failing tests** for both fixed profiles, accepted qualifying candidates, rejected missing evidence, numeric normalization, filter boundaries, duplicate canonical URLs, and favorite persistence.
- [ ] **Step 2: Run `python3 -m unittest tests.test_auto_market -v` and verify the new tests fail for missing module/functions.**
- [ ] **Step 3: Implement the smallest stdlib-only domain module** with normalized integer/float fields, explicit evidence checks, a SQLite database path from `AUTO_MARKET_DB` or `auto-market.sqlite3`, and tables for `listings`, `favorites`, and `refresh_runs`.
- [ ] **Step 4: Implement `refresh_listings(discover, fetch)`** so discovery candidates are normalized and upserted, known listings are rechecked, confirmed unavailable listings are removed, and provider failures are recorded without deleting active rows.
- [ ] **Step 5: Run the focused test file and then `python3 -m unittest tests.test_auto_market -v`; verify PASS.**

### Task 2: Protected API and screener page

**Files:**
- Modify: `server.py` in `Handler.do_GET`, `Handler.do_POST`, and dashboard auth helpers
- Modify: `index.html` navigation
- Create: `auto-market.html`
- Create: `auto-market.js`
- Create: `tests/test_auto_market_http.py`

**Interfaces:**
- Consumes Task 1's `auto_connect`, `list_active_listings`, `set_favorite`, and filter constants.
- Produces `GET /api/auto-market/state`, `POST /api/auto-market/favorite`, and `POST /api/auto-market/login` using JSON and the existing CSRF pattern. `state` returns `profiles`, `filters`, `listings`, `last_refresh`, and `next_refresh`.

- [ ] **Step 1: Write failing HTTP tests** for unauthenticated access, password login from `AUTO_MARKET_PASSWORD`, CSRF-protected state/favorite calls, editable filter bounds, and a favorite visible after a fresh authenticated request.
- [ ] **Step 2: Run `python3 -m unittest tests.test_auto_market_http -v` and verify the new tests fail.**
- [ ] **Step 3: Add the minimal single-user session** following the existing finance session pattern, with `HttpOnly`, `SameSite=Strict` cookie and a per-session CSRF token; do not create multi-user registration.
- [ ] **Step 4: Add the three JSON routes** and validate integer filters, request size, listing IDs, and boolean favorite values at the trust boundary.
- [ ] **Step 5: Build `auto-market.html` and `auto-market.js`** with both sections on one responsive page, default filters of 250 km and 40,000 EUR, card previews, original links, image fallback, favorite controls, last-update state, and accessible empty/loading/error states.
- [ ] **Step 6: Add the `Auto Screener` navigation entry and route serving, then run the focused HTTP tests and `python3 -m unittest -v`; verify PASS.**

### Task 3: Discovery, source registry, and scheduled refresh

**Files:**
- Create: `auto_market_sources.py`
- Create: `auto_market_refresh.py`
- Create: `auto-market-refresh.service`
- Create: `auto-market-refresh.timer`
- Create: `tests/test_auto_market_sources.py`

**Interfaces:**
- Consumes Task 1's `normalize_candidate` and `refresh_listings`.
- Produces `discover_candidates() -> list[dict]`, `fetch_listing(url: str) -> dict`, and `run_refresh() -> dict`.

- [ ] **Step 1: Write failing parser tests** for JSON-LD/structured listing data, explicit accident-free and long-version evidence, canonical URL extraction, image extraction, unsupported domains, and malformed provider responses.
- [ ] **Step 2: Run `python3 -m unittest tests.test_auto_market_sources -v` and verify the new tests fail.**
- [ ] **Step 3: Implement a configured discovery client** using `AUTO_MARKET_SEARCH_URL` and optional `AUTO_MARKET_SEARCH_TOKEN`; send profile queries including Norderstedt, 250 km, long version, 2025+, 40,000 km, 40,000 EUR, and accident-free terms, with bounded timeouts and no credentials in URLs or logs.
- [ ] **Step 4: Implement an explicit trusted-source registry and stdlib HTML/JSON-LD parser**; discard candidates whose source domain or required evidence is not accepted. Keep source adapters small and data-driven so another source is one bounded addition.
- [ ] **Step 5: Implement `run_refresh()` and the systemd timer** with four local runs at 00:00, 06:00, 12:00, and 18:00 Europe/Berlin, plus a bounded lock so overlapping runs do not corrupt the SQLite database.
- [ ] **Step 6: Run source tests, invoke one refresh against a fixture provider, and verify the timer/service files contain no secrets and the refresh result reports inserted, updated, removed, and failed counts.**

### Task 4: Integration verification and handoff

**Files:**
- Modify: `docs/superpowers/specs/2026-11-10-auto-market-screener-design.md` status
- Modify: `HERMES.md` only if the refresh configuration needs a documented environment variable entry

- [ ] **Step 1: Run `python3 -m unittest -v` and fix only regressions caused by the screener.**
- [ ] **Step 2: Run a local dashboard smoke check** that logs in, loads both profiles, changes the global filters, favorites one listing, starts a new session, and confirms the favorite remains.
- [ ] **Step 3: Check responsive markup, keyboard access, external-link safety, missing-image fallback, and no-results messaging at desktop and mobile widths.**
- [ ] **Step 4: Update the design status to implemented and record any missing live search credential or source limitation explicitly.**

## Execution Notes

- Keep commits small and task-scoped; do not reformat unrelated files.
- If the live search provider is not configured, the page must still load and report the refresh as unavailable rather than showing invented or fixture listings.
- Do not claim broad live coverage until at least one configured discovery run and one source re-check succeed against real public pages.
