# Independent Betting Forecast Design

**Status:** Design approved in chat; written-spec review pending.

## Outcome

Replace circular market-derived `PAPER` candidates with independent, paper-only forecasts for football and NFL. The system compares its own probability against current bookmaker prices, emits a candidate only when data quality, win probability, uncertainty, and expected value all pass conservative gates, and records every decision for walk-forward evaluation.

Profit is not guaranteed. No real-money placement, bookmaker account integration, deposit, or mainnet-like activation is in scope.

## Current Root Cause

`/home/yash/dashboard/server.py` currently derives `model_probability` from the same bookmaker prices later used for edge and expected-value calculations. This is line shopping, not an independent forecast. Current odds snapshots contain reliable market data but no persisted team features, news, injuries, lineups, weather, model version, or closing-line record.

## Scope

### In scope

- Football competitions already configured in `SPORTS` and NFL.
- Independent, sport-specific probability estimates that do not consume bookmaker odds as model inputs.
- Timestamped source observations with source name, URL or endpoint ID, retrieval time, and completeness status.
- Conservative candidate gating that rejects unsupported longshots and stale or incomplete information.
- Persistent forecast journal with immutable decision-time inputs, model output, selected price, outcome, and closing price when available.
- Paper-only UI showing probability, fair quote, market quote, uncertainty, data sources, model version, and no-call reasons.
- Walk-forward evaluation using settled paper outcomes, Brier score, log loss, ROI, calibration, and closing-line value.

### Out of scope

- Real-money wagering or bookmaker API actions.
- Guaranteed profitability, autonomous model promotion, or claims based on small samples.
- Blind scraping of arbitrary websites, unverified social posts, or fabricated missing data.
- New paid services or committed secrets.
- Expansion to additional sports before football and NFL validation gates pass.

## Architecture

```text
source adapters
  -> normalized feature snapshot
  -> sport-specific independent model
  -> uncertainty and data-quality gate
  -> odds comparison
  -> paper candidate or explicit NO_CALL
  -> immutable forecast journal
  -> settlement and walk-forward metrics
```

The existing odds snapshot remains the comparison input and source for available prices. It must not be passed into the independent prediction model. Each model receives only event identity, sport data, team or player features, and source metadata.

## Source Contract

Each adapter returns normalized observations with:

- `event_id`, competition, participants, and scheduled start;
- feature name, value, unit, and observation timestamp;
- source identifier and retrieval timestamp;
- source status: `available`, `partial`, or `unavailable`;
- stable raw-payload hash for audit without storing unnecessary raw content.

The initial registry uses only configured or freely accessible sources that can be queried lawfully and reliably. Odds-only data cannot satisfy an independent feature requirement. Missing, stale, contradictory, or unverified observations reduce completeness; if the minimum feature set is not present, result is `NO_CALL` with a concrete reason.

## Models

### Football

Use a simple independently trained baseline: time-decayed team attack and defense ratings, home advantage, and a Poisson goal model. Produce outcome probabilities for home win, draw, and away win. Form and injuries may adjust ratings only when normalized source observations exist; otherwise they are absent, not guessed.

### NFL

Use time-decayed team strength with home advantage and a calibrated logistic outcome model. Add injury, quarterback, rest, and weather features only when source observations are present and timestamped. Missing high-impact information forces `NO_CALL` when uncertainty exceeds the configured gate.

### Common output

Every forecast returns:

- `model_version`;
- probabilities summing to one;
- fair quote per selection;
- uncertainty interval or uncertainty score;
- feature completeness and source list;
- rationale containing model inputs, not generic prose;
- explicit `PAPER` or `NO_CALL` status.

## Candidate Gates

A candidate is paper-eligible only if all gates pass:

1. Event and market are supported by the model.
2. Required independent feature set is complete and fresh.
3. Forecast probability passes the sport/market probability floor.
4. Uncertainty-adjusted probability still produces positive expected value at the selected price.
5. Minimum edge and maximum price gates pass; high quote alone never qualifies.
6. Model version has a recorded validation status; unvalidated models remain `NO_CALL`.

The UI must show rejected events and the exact first failed gate. A high-odds outsider with weak win probability remains `NO_CALL` even when raw expected value appears positive.

## Persistence and Learning

Extend the existing betting SQLite database with:

- normalized feature snapshots;
- model runs and versions;
- forecasts keyed by event, market, selection, and snapshot;
- decision-time odds and later closing odds;
- paper settlements and source provenance.

Rows are append-only after publication except settlement fields. Re-running a snapshot is idempotent by source/event/time identity. Training uses only completed historical rows and time-ordered walk-forward splits. No model is promoted from its own in-sample results.

The system reports `UNAVAILABLE` until minimum sample and calibration requirements are met. It does not turn a positive paper ROI from a small sample into a profitability claim.

## Scheduler and Failure Handling

The existing 30-minute timer refreshes odds and feature snapshots. A failed adapter records the failure and preserves the last known snapshot as stale; it does not generate candidates from stale or incomplete data. Timeout, rate limit, malformed payload, and schema mismatch become visible source errors. One source failure must not erase unrelated source history.

## UI Contract

Replace circular wording such as `Fair` from market consensus with:

- `Eigene Prognose`;
- `Gewinnchance`;
- `Faire Modellquote`;
- `Beste Marktquote`;
- `Unsicherheit`;
- `Datenstand` and source list;
- `PAPER` or `NO_CALL` plus reason.

Paper journal and manual settlement remain. The page must never offer a placement action or imply that a candidate is profitable. If no candidate passes, display a useful no-call explanation rather than an empty loading state.

## Validation and Acceptance

Acceptance requires:

- a test proving bookmaker prices do not change model probability when independent features stay constant;
- tests for football and NFL probability normalization and missing-data rejection;
- tests for longshot rejection, uncertainty-adjusted EV, stale data, source failure, and idempotent snapshots;
- walk-forward fixtures with known outcomes and metrics;
- HTTP and UI tests proving source provenance, model version, status, and no-call reason are visible;
- full existing test suite, Python compilation, diff checks, and browser checks passing;
- live paper snapshot showing independent model status and zero real-money actions.

The first release is accepted only as a paper research instrument. Real-money enablement requires a separate explicit design and evidence review after independent out-of-sample validation.

