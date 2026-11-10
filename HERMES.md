# Dashboard Hermes Runbook

## Scope

- Worktree: `/home/yash/dashboard`
- Branch: `hermes-stabilize-dashboard-renewal`
- Dashboard: `systemctl --user restart dashboard.service`, port `8787`
- Browser QA: `/home/yash/browser-qa/venv/bin/python /home/yash/browser-qa/run_dashboard_qa.py`
- Snapshot timer: `betting-snapshot.timer`, paper-only; never enable Echtgeld or Mainnet trading.

## Auto Market Screener

- Configure `AUTO_MARKET_SEARCH_URL` in `~/.hermes/.env`; `AUTO_MARKET_SEARCH_TOKEN` is optional.
- The search endpoint must return JSON with a `results` list containing public listing URLs and `distance_km` values from Norderstedt.
- Enable the refresh schedule with `systemctl --user enable --now auto-market-refresh.timer` after installing the unit files.

## Completion

1. Read `AGENTS.md`, `HERMES.md`, `git status`, and relevant diffs before editing.
2. Preserve unrelated tracked and untracked user work. Never stage runtime databases, backups, `.env`, or secrets.
3. Trace the real data path before changing code. Make the smallest safe fix.
4. Run focused tests, then the full local suite, compile checks, and `git diff --check`.
5. For service or UI changes, check the live API and service, then run desktop/mobile browser QA with the installed runtime.
6. Review the complete diff yourself. A worker exit code or marker response is not proof of completion.
7. Report exact status, tests, evidence, remaining blockers, and commit. Never claim PASS without current evidence.

## Safety

- No secrets in output or commits.
- No destructive deletion, public exposure, paid action, financial transaction, real-money betting, or Mainnet trading.
- Keep failed, stale, unavailable, and unvalidated states explicit. Never replace missing evidence with fake data.
- Use existing native Hermes CLI and dashboard adapters. Do not invent a second orchestration or recovery chain.
