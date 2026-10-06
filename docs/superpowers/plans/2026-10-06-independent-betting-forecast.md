# Independent Betting Forecast Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace circular odds-consensus candidates with independently generated, paper-only football and NFL forecasts that expose evidence, reject unsupported bets, and learn through time-ordered evaluation.

**Architecture:** Keep The Odds API as comparison-only input. Add a small stdlib source-normalization module, pure sport-specific forecast functions, SQLite persistence for feature/model/forecast provenance, and a scheduler pipeline that produces either a gated `PAPER` candidate or explicit `NO_CALL`.

**Tech Stack:** Python 3 standard library, `urllib`, `csv`, `sqlite3`, existing `http.server`, existing browser JavaScript, systemd user timer. No new dependency and no paid source.

**Spec:** `docs/superpowers/specs/2026-10-06-independent-betting-forecast-design.md`

## Global Constraints

- The Odds API prices are comparison-only and never model inputs.
- Missing, stale, contradictory, or unverified source data produces `NO_CALL`.
- No real-money placement, bookmaker account action, deposit, or mainnet activation.
- No new dependency, paid service, secret, or committed runtime data.
- Existing paper journal, CSRF checks, user files, and paused auth work remain intact.
- All new behavior starts with a failing test and uses minimal stdlib code.

## Review Focus

- Changed bookmaker price with fixed independent features: model probability stays identical; test in Task 2.
- Missing or stale source observation: forecast is `NO_CALL` with source-specific reason; tests in Tasks 1 and 3.
- High-odds outsider with low win probability: rejected despite positive raw EV; test in Task 2.
- Duplicate timer run: one forecast identity is stored; test in Task 3.
- Source timeout or malformed payload: old data is marked stale and no new candidate is emitted; test in Task 4.

### Task 1: Normalize independent source observations

**Files:**
- Create: `/home/yash/dashboard/betting_sources.py`
- Modify: `/home/yash/dashboard/test_status.py`

**Interfaces:**
- Consumes: odds event identity from `server.normalize_event`, configured source URLs, and current time.
- Produces: `source_snapshot(event: dict, sport: str, now: datetime, fetch_json=None) -> dict` with `status`, `observations`, `sources`, `retrieved_at`, `completeness`, and `errors`.

- [x] **Step 1: Write failing tests**

  Add tests for normalized observation shape, source timestamps, malformed payload rejection, stale observation rejection, and a source failure that returns `unavailable` without raising.

- [x] **Step 2: Run tests and verify expected failure**

  Run: `python3 -m unittest -v test_status.StatusTests.test_independent_source_contract`

  Expected: FAIL because `betting_sources.source_snapshot` does not exist.

- [x] **Step 3: Implement minimal source adapters**

  Implement stdlib HTTP adapters with a 10-second timeout and bounded response size. Use the existing odds feed only for event identity. Initial sources are OpenLigaDB match results/tables for `bl1`, `bl2`, and `bl3`, nflverse `https://github.com/nflverse/nflverse-data/releases/download/schedules/games.csv` for NFL historical games, and Open-Meteo only when a verified venue coordinate is available. Allow endpoint overrides through task-specific environment variables. Identify each source by name and endpoint ID, hash payloads, and return `partial` or `unavailable` instead of fabricating values. Do not log keys or raw secrets.

- [x] **Step 4: Run focused and full tests**

  Run: `python3 -m unittest -v test_status.StatusTests.test_independent_source_contract`

  Expected: PASS.

- [x] **Step 5: Commit**

  ```bash
  git add betting_sources.py test_status.py
  git commit -m "feat: normalize betting source observations"
  ```

### Task 2: Add independent football and NFL forecasts

**Files:**
- Create: `/home/yash/dashboard/betting_forecast.py`
- Modify: `/home/yash/dashboard/test_status.py`

**Interfaces:**
- Consumes: normalized observations from `source_snapshot`, event identity, and model constants.
- Produces: `forecast_football(event, observations) -> dict`, `forecast_nfl(event, observations) -> dict`, and `gate_forecast(forecast, price) -> dict`.

- [x] **Step 1: Write failing tests**

  Add fixtures with fixed historical results and context. Assert football three-way probabilities sum to `1.0`, NFL probabilities sum to `1.0`, the same observations yield the same probability for different bookmaker prices, missing minimum data returns `NO_CALL`, and a low-probability high-price outsider fails the probability/uncertainty gate.

- [x] **Step 2: Run tests and verify expected failure**

  Run: `python3 -m unittest -v test_status.StatusTests.test_independent_forecast_contract`

  Expected: FAIL because forecast functions do not exist.

- [x] **Step 3: Implement minimal pure models**

  Implement a time-decayed football attack/defense plus home-advantage Poisson baseline and a time-decayed NFL team-strength logistic baseline. Use only observations; never read odds. Return `model_version`, probabilities, fair quotes, uncertainty, completeness, source list, and evidence-based rationale. Keep context adjustments bounded and ignore absent features.

- [x] **Step 4: Implement conservative candidate gates**

  `gate_forecast` must reject unsupported markets, incomplete/stale data, unvalidated models, probabilities below configured sport floors, excessive uncertainty, negative uncertainty-adjusted EV, minimum-edge failures, and prices above the configured longshot cap. Return first failed gate in `reason`.

- [x] **Step 5: Run focused and full tests**

  Run: `python3 -m unittest -v test_status.StatusTests.test_independent_forecast_contract`

  Expected: PASS, then run `python3 -m unittest -v test_status` and keep the existing suite green.

- [x] **Step 6: Commit**

  ```bash
  git add betting_forecast.py test_status.py
  git commit -m "feat: add independent paper forecast models"
  ```

### Task 3: Persist feature snapshots, model runs, and forecasts

**Files:**
- Modify: `/home/yash/dashboard/server.py`
- Modify: `/home/yash/dashboard/test_status.py`

**Interfaces:**
- Consumes: `source_snapshot`, `forecast_football`, `forecast_nfl`, and `gate_forecast`.
- Produces: `save_feature_snapshot`, `save_model_run`, `save_forecast`, `latest_forecasts`, and `forecast_metrics` using the existing `BETTING_DB`.

- [x] **Step 1: Write failing tests**

  Add tests for schema creation in a temporary DB, append-only forecast rows, idempotent `(snapshot_id,event_id,market,selection,model_version)` storage, provenance retrieval, and metrics returning `UNAVAILABLE` below the minimum settled sample.

- [x] **Step 2: Run tests and verify expected failure**

  Run: `python3 -m unittest -v test_status.StatusTests.test_forecast_persistence_contract`

  Expected: FAIL because the persistence functions and tables do not exist.

- [x] **Step 3: Implement SQLite persistence**

  Add only the required tables and indexes. Store compact normalized JSON for observations and model output, source hashes, decision-time odds, model version, gate status, and reason. Preserve existing `odds_snapshots` and `paper_bets` behavior. Use read-only connections for state reads and explicit transactions for writes.

- [x] **Step 4: Implement walk-forward metrics**

  Calculate Brier score, log loss, ROI, calibration buckets, and CLV only from settled paper rows with time-ordered decision timestamps. Return `UNAVAILABLE` until the configured minimum sample is reached; never label a small sample profitable.

- [x] **Step 5: Run focused and full tests**

  Run: `python3 -m unittest -v test_status.StatusTests.test_forecast_persistence_contract test_status`

  Expected: PASS.

- [x] **Step 6: Commit**

  ```bash
  git add server.py test_status.py
  git commit -m "feat: persist forecast provenance and metrics"
  ```

### Task 4: Replace snapshot pipeline and fail closed

**Files:**
- Modify: `/home/yash/dashboard/betting_snapshot.py`
- Modify: `/home/yash/dashboard/server.py`
- Modify: `/home/yash/dashboard/test_status.py`

**Interfaces:**
- Consumes: latest odds snapshot, source snapshots, sport models, persistence functions.
- Produces: `refresh_betting_snapshot()` with `forecast_status`, independent candidates, no-call reasons, and source errors.

- [x] **Step 1: Write failing tests**

  Add tests proving a successful refresh stores a forecast, a source timeout preserves old data as stale, a malformed response produces no candidate, and a repeated refresh does not duplicate forecasts.

- [x] **Step 2: Run tests and verify expected failure**

  Run: `python3 -m unittest -v test_status.StatusTests.test_forecast_refresh_contract`

  Expected: FAIL because refresh still calls circular `baseline_model`.

- [x] **Step 3: Implement pipeline replacement**

  Keep `normalize_event` focused on event/market normalization. Call source adapters, select the sport model, persist feature/model/forecast rows, compare prices only after forecasting, and expose `PAPER` or `NO_CALL`. Remove the old circular model call after new tests cover the replacement. Keep timer cadence at 30 minutes and keep service exit status nonzero only when no usable snapshot exists.

- [x] **Step 4: Run focused, full, and compile checks**

  Run: `python3 -m unittest -v test_status`; `python3 -m py_compile server.py betting_sources.py betting_forecast.py betting_snapshot.py`; `git diff --check`.

  Expected: all tests pass, compile succeeds, and diff check is empty.

- [x] **Step 5: Commit**

  ```bash
  git add server.py betting_snapshot.py test_status.py
  git commit -m "feat: run independent forecasts in paper snapshot"
  ```

### Task 5: Expose evidence in Betting Lab

**Files:**
- Modify: `/home/yash/dashboard/betting.html`
- Modify: `/home/yash/dashboard/betting.js`
- Modify: `/home/yash/dashboard/betting.css`
- Modify: `/home/yash/dashboard/test_status.py`

**Interfaces:**
- Consumes: `/api/betting/state` forecast fields and existing paper journal endpoints.
- Produces: accessible cards/table with own probability, fair model quote, market quote, uncertainty, source freshness, model version, status, and exact no-call reason.

- [x] **Step 1: Write failing contract tests**

  Assert the UI contains `Eigene Prognose`, `Gewinnchance`, `Unsicherheit`, `Datenstand`, `model_version`, and `NO_CALL` reason handling, and contains no copy implying guaranteed profit or real-money placement.

- [x] **Step 2: Run test and verify expected failure**

  Run: `python3 -m unittest -v test_status.StatusTests.test_independent_forecast_ui_contract`

  Expected: FAIL because current UI says `PAPER ONLY` and `Kein unabhängiges Vorhersagemodell`.

- [x] **Step 3: Implement compact evidence rendering**

  Replace circular `Fair` labels with model-specific labels, render source and freshness state, show first no-call reason, preserve filters and manual paper settlement, escape all dynamic values, and keep mobile layout compact. Do not add placement controls.

- [x] **Step 4: Run tests and browser checks**

  Run: `python3 -m unittest -v test_status`; execute existing Playwright/browser QA at desktop and mobile viewports against `/betting`.

  Expected: all HTTP routes return `200`, no console/page/request errors, no overflow, and evidence values match the tested snapshot.

- [x] **Step 5: Commit**

  ```bash
  git add betting.html betting.js betting.css test_status.py
  git commit -m "feat: show independent forecast evidence"
  ```

### Task 6: Live paper validation and handoff

**Files:**
- Modify only if required by verified findings: `server.py`, `betting_snapshot.py`, `betting.html`, `betting.js`, `betting.css`, or `test_status.py`

- [x] **Step 1: Run complete verification**

  Run the full unit suite with warnings as errors, Python compilation, `git diff --check`, live `/api/betting/state`, `/betting` HTTP checks, and existing desktop/mobile browser QA.

- [x] **Step 2: Run one real paper snapshot**

  Run the existing systemd snapshot service once, verify logs show source status, model version, event count, candidate/no-call counts, and no real-money action.

- [x] **Step 3: Inspect acceptance gates**

  Confirm model validation status is visible, metrics remain `UNAVAILABLE` until sample threshold, all candidates have independent provenance, and stale/failed sources yield `NO_CALL`.

- [x] **Step 4: Commit only verified fixes**

  Use one focused commit per verified fix. Do not stage untracked user files, SQLite runtime data, backups, or secrets.

- [x] **Step 5: Push the branch and report evidence**

  Push `hermes-stabilize-dashboard-renewal`, report commit IDs, live URL, source status, screenshots, paper metrics, and remaining external blockers. Do not enable Echtgeld.

