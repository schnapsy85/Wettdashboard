# Meine Wetten – Hermes Dashboard

Implementation branch: \`feature/meine-wetten-html-results\`; PR #2; specification: issue #1.

## Architecture

- \`/wetten\` serves \`wetten.html\` / \`wetten.js\` / \`wetten.css\`.
- \`/api/wetten/session|login|data|create|settle|delete|logout\`: private JSON endpoints integrated into the existing \`server.py\`.
- \`wetten_core.py\`: SQLite ticket/leg/audit storage, payout calculations, manual settlement and background HTML-result checker every 300 seconds.
- Existing \`/betting\` Betting Lab remains unchanged except for navigation.
- **No external sports data API is used**. This is not an automated bookmaker login and never places a bet.

## Privacy and authentication

On an authorized Hermes machine, set a unique strong environment secret (minimum 12 characters, preferably 24+) in the existing systemd service environment, outside Git:

\`\`\`
WETTEN_PASSWORD=<unique, strong secret>
WETTEN_DB_PATH=/home/yash/.local/share/hermes-wetten/wetten.sqlite3
WETTEN_COOKIE_SECURE=1
WETTEN_HTML_ALLOW_HOSTS=
\`\`\`

Never commit or publish the password or actual betting records.

- \`WETTEN_COOKIE_SECURE=1\` is recommended for the actual HTTPS dashboard. Local HTTP-only test environments may set \`0\`, **but never publish an HTTP dashboard**.
- No password => fail-closed: login unavailable, saved bets inaccessible.
- Private API requires a per-session CSRF token for state-changing requests, with HttpOnly SameSite=Strict cookie.
- Restrict dashboard network access to owner-controlled authenticated routes or a private network. Check Cloudflare cache rules for private requests (\`Cache-Control: no-store\`).
- Journal is saved in SQLite, not browser localStorage; automatic daily SQLite snapshots are saved in a private \`backups/wetten/\` directory adjacent to the DB.
- No real customer/ticket/bet data is embedded in GitHub source or tests.
- **This is currently a draft PR, not installed on the live Hermes host.**

## API-free result checking

The checker always wakes every 300 seconds, even with closed browser. To protect external sites, it does **nothing outbound by default** and displays \`disabled_no_approved_html_sources\`. Results are not fabricated.

For optional live settlement of supported markets, the owner must:
1. Select a site explicitly allowing automated retrieval of public HTML, check its published terms and \`robots.txt\`.
2. Add the site's hostname to \`WETTEN_HTML_ALLOW_HOSTS\` in the server environment. Examples in documentation are deliberately omitted: NFL and some league sites restrict systematic scraping, and no permission is assumed.
3. Provide a precise public HTTPS game-result URL in each bet leg.
4. Verify parsing fixtures against that source's *actual* visible final-score HTML. The generic parser currently only accepts a single adjacent \`Home SCORE - SCORE Away FINAL\` pattern. It is intentionally conservative: most real-world pages require separately reviewed per-site HTML adapters. If no robust legal adapter exists, settle that selection manually.

The checker uses \`robots.txt\`, refuses redirects, refuses unapproved hosts, limits response size and timeout, de-duplicates same URLs per run and stores evidence. It does not use JSON/XHR score endpoints, bookmaker pages, proprietary sports APIs, paywall or captcha bypasses.

**A 300-second check cadence is not equivalent to guaranteed results within 300 seconds.**

## Verification in isolated clone/worktree

\`\`\`bash
python3 -m py_compile server.py wetten_core.py test_wetten.py test_wetten_api.py
python3 -m unittest -v test_wetten.py test_wetten_api.py
\`\`\`

In CI the tests run with local, temporary SQLite and localhost HTTP. The live site and production DB are not touched.

## Safe installation (operator / locally connected Hermes)

1. Check \`systemctl cat dashboard.service\`, \`git status\`, running version and backups on the actual host. Don't overwrite dirty files, do not modify Finanzjunkie.
2. Review PR #2 diff, CI, runtime safeguards and web-source terms, then merge into \`master\` **only after approval**.
3. Create a separate isolated preview/worktree of \`master\` on Hermes and run tests before integrating.
4. Configure the password/DB path/HTTPS cookie settings in a root-controlled systemd environment file with restrictive permissions.
5. Backup existing dashboard and DB, deploy reviewed code via the established local deployment process, and restart **only** the dashboard service. Verify the page and API via private HTTPS, including bad password/anonymous denial and persistence across restart.
6. Keep \`WETTEN_HTML_ALLOW_HOSTS\` empty until at least one lawful and robust HTML site adapter has passed real final-score verification. Configure and test hosts one at a time.
7. Validate actual result evidence and status; only then mark automated settlement as operational.

A PR passing isolated tests is **not** proof of production deployment or functioning live source coverage.

## Known limitations

- No edit UI, screenshot OCR or bookmaker-account import. Users can manually create/delete/settle tickets.
- CSV import and richer market-specific adapters remain future work.
- Score patterns and match rules vary by sport and bookmaker. Unsupported/special markets must remain \`needs_review\`.
- The HTML updater cannot automatically settle matches if legally permitted, machine-readable public result pages are unavailable.
- Verify security of the actual Cloudflare/private dashboard deployment before exposing financial bet data.
