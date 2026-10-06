#!/usr/bin/env python3
import csv, hashlib, hmac, io, json, math, os, secrets, shutil, socket, sqlite3, subprocess, time, urllib.request
from collections import defaultdict
from contextlib import closing
from datetime import date, datetime
from urllib.parse import parse_qs, urlencode, urlsplit, quote
import hyperliquid_bot
import betting_forecast
import betting_sources

FANTASY_API = os.environ.get('FANTASY_API_URL', 'http://127.0.0.1:8091').rstrip('/')

def fantasy_data(query):
    try:
        headers = {'User-Agent': 'Hermes-Dashboard/1.0'}
        base = FANTASY_API + '/intelligence/league/1389346968114851840?'
        with urllib.request.urlopen(urllib.request.Request(base + urlencode(query), headers=headers), timeout=20) as response:
            data = json.loads(response.read())
        report_q = {k: query[k] for k in ('username', 'season', 'week') if k in query}
        report_url = FANTASY_API + '/intelligence/league/1389346968114851840/scoring-report?' + urlencode(report_q)
        lineup_url = FANTASY_API + '/intelligence/league/1389346968114851840/lineup?' + urlencode(report_q)
        status_url = FANTASY_API + '/sleeper/projections/' + str(query.get('season', 2026)) + '/' + str(query.get('week', 4))
        try:
            with urllib.request.urlopen(urllib.request.Request(lineup_url, headers=headers), timeout=20) as response:
                data['lineup_intelligence'] = json.loads(response.read())
        except Exception as exc:
            data['lineup_intelligence'] = {'status': 'ERROR', 'error': type(exc).__name__}
        try:
            with urllib.request.urlopen(urllib.request.Request(report_url, headers=headers), timeout=20) as response:
                data['scoring_report'] = json.loads(response.read())
            with urllib.request.urlopen(urllib.request.Request(status_url, headers=headers), timeout=20) as response:
                projection_status = json.loads(response.read())
            data['scoring_report']['status'] = projection_status.get('status', 'MISSING')
            data['scoring_report']['projection_status'] = projection_status
        except Exception as exc:
            data['scoring_report'] = {'status': 'ERROR', 'error': type(exc).__name__}
        return data
    except Exception as exc:
        return {'error': 'Fantasy Backend nicht erreichbar', 'reason': type(exc).__name__}

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).parent
def hermes_home():
    return Path(os.environ.get('HERMES_HOME', str(Path.home() / '.hermes'))).expanduser()
FINANCE_DB = ROOT / 'finanzen.sqlite3'

JARVIS_DEFAULT_CONFIG = {
    'theme': {'bg': '#0a0b1e', 'surface': '#15162e', 'cyan': '#09d6ff', 'text': '#f7f8ff'},
    'layout': ['mission-control', 'voice-link', 'data-policy'],
}

def jarvis_config():
    """Return bounded local config; malformed or missing config fails closed."""
    path = ROOT / 'jarvis.config.json'
    try:
        if not path.is_file():
            return {**JARVIS_DEFAULT_CONFIG, 'theme': dict(JARVIS_DEFAULT_CONFIG['theme']), 'layout': list(JARVIS_DEFAULT_CONFIG['layout']), 'source': 'defaults'}
        raw = json.loads(path.read_text())
        theme = raw.get('theme', {}) if isinstance(raw, dict) else {}
        layout = raw.get('layout', []) if isinstance(raw, dict) else []
        if not isinstance(theme, dict) or not isinstance(layout, list): raise ValueError
        colors = {k: v for k, v in theme.items() if k in JARVIS_DEFAULT_CONFIG['theme'] and isinstance(v, str) and len(v) <= 32}
        widgets = [x for x in layout if isinstance(x, str) and x in JARVIS_DEFAULT_CONFIG['layout']]
        return {'theme': {**JARVIS_DEFAULT_CONFIG['theme'], **colors}, 'layout': widgets or JARVIS_DEFAULT_CONFIG['layout'], 'source': 'local'}
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {**JARVIS_DEFAULT_CONFIG, 'theme': dict(JARVIS_DEFAULT_CONFIG['theme']), 'layout': list(JARVIS_DEFAULT_CONFIG['layout']), 'source': 'defaults'}
FINANCE_BACKUP_DIR = ROOT / 'backups'
FINANCE_SESSIONS = {}
FINANCE_PREVIEWS = {}
FINANCE_MAX_UPLOAD = 5 * 1024 * 1024

FINANCE_ACCOUNTS = (
    ('Trade Republic Kreditkarte', 'creditcard'), ('comdirect Girokonto', 'checking'),
    ('DKB Girokonto', 'checking'), ('DKB Kreditkarte', 'creditcard'), ('N26 Girokonto', 'checking'),
)

def finance_connect():
    con = sqlite3.connect(FINANCE_DB)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys = ON')
    con.executescript('''
    CREATE TABLE IF NOT EXISTS accounts (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL, kind TEXT NOT NULL, currency TEXT NOT NULL DEFAULT 'EUR', active INTEGER NOT NULL DEFAULT 1);
    CREATE TABLE IF NOT EXISTS imports (id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL, filename TEXT NOT NULL, created_at TEXT NOT NULL, status TEXT NOT NULL, row_count INTEGER NOT NULL DEFAULT 0, FOREIGN KEY(account_id) REFERENCES accounts(id));
    CREATE TABLE IF NOT EXISTS transactions (id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL, import_id INTEGER, booked_on TEXT NOT NULL, value_date TEXT, description TEXT NOT NULL, amount_cents INTEGER NOT NULL, kind TEXT NOT NULL, review_status TEXT NOT NULL, fingerprint TEXT NOT NULL, created_at TEXT NOT NULL, FOREIGN KEY(account_id) REFERENCES accounts(id), FOREIGN KEY(import_id) REFERENCES imports(id));
    CREATE UNIQUE INDEX IF NOT EXISTS transactions_fingerprint ON transactions(account_id, fingerprint);
    CREATE TABLE IF NOT EXISTS rules (id INTEGER PRIMARY KEY, name TEXT NOT NULL, pattern TEXT NOT NULL, category TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1);
    CREATE TABLE IF NOT EXISTS audit_log (id INTEGER PRIMARY KEY, action TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL);
    ''')
    for name, kind in FINANCE_ACCOUNTS:
        con.execute('INSERT OR IGNORE INTO accounts(name, kind) VALUES (?, ?)', (name, kind))
    con.commit()
    return con

def finance_backup():
    if not FINANCE_DB.exists(): return
    stamp = datetime.now().strftime('%Y-%m')
    target = FINANCE_BACKUP_DIR / f'finanzen-{stamp}.sqlite3'
    if not target.exists():
        FINANCE_BACKUP_DIR.mkdir(exist_ok=True)
        shutil.copy2(FINANCE_DB, target)

def finance_audit(con, action, detail):
    con.execute('INSERT INTO audit_log(action, detail, created_at) VALUES (?, ?, ?)', (action, detail[:500], datetime.now().isoformat(timespec='seconds')))

def finance_password_ok(value):
    configured = os.environ.get('FINANCE_PASSWORD', '')
    return bool(configured) and hmac.compare_digest(str(value), configured)

def finance_session(handler):
    cookie = handler.headers.get('Cookie', '')
    sid = next((part.split('=', 1)[1] for part in cookie.split('; ') if part.startswith('finance_session=')), None)
    return FINANCE_SESSIONS.get(sid)

def finance_require(handler, csrf=False):
    session = finance_session(handler)
    if not session:
        handler.send_json({'error': 'Anmeldung erforderlich'}, 401); return None
    if csrf and not hmac.compare_digest(handler.headers.get('X-CSRF-Token', ''), session['csrf']):
        handler.send_json({'error': 'CSRF-Prüfung fehlgeschlagen'}, 403); return None
    return session

def finance_date(value):
    value = value.strip()
    for fmt in ('%d.%m.%Y', '%d.%m.%y', '%Y-%m-%d', '%Y/%m/%d', '%d/%m/%Y', '%m/%d/%Y'):
        try: return datetime.strptime(value, fmt).date().isoformat()
        except ValueError: pass
    raise ValueError('Ungültiges Datum')

def finance_amount(value):
    raw = value.strip().replace('\xa0', '').replace(' ', '')
    raw = raw.replace('EUR', '').replace('€', '')
    if ',' in raw and '.' in raw: raw = raw.replace('.', '').replace(',', '.')
    elif ',' in raw: raw = raw.replace(',', '.')
    return int(round(float(raw) * 100))

def finance_parse_csv(raw, account_kind):
    text = None
    for encoding in ('utf-8-sig', 'cp1252'):
        try: text = raw.decode(encoding); break
        except UnicodeDecodeError: pass
    if text is None: raise ValueError('CSV-Encoding nicht erkannt; UTF-8 oder Windows-1252 verwenden')
    sample = text[:4096]
    try: dialect = csv.Sniffer().sniff(sample, delimiters=',;\t')
    except csv.Error: dialect = csv.excel; dialect.delimiter = ';' if sample.count(';') > sample.count(',') else ','
    rows = list(csv.reader(io.StringIO(text), dialect))
    if not rows: raise ValueError('CSV ist leer')
    headers = [x.strip().lower() for x in rows[0]]
    def find(*names):
        for name in names:
            for i, header in enumerate(headers):
                if name in header: return i
        return None
    di, ai, ti = find('buchungstag', 'buchungsdatum', 'datum', 'date'), find('betrag', 'amount', 'umsatz'), find('verwendungszweck', 'beschreibung', 'description', 'payee', 'merchant')
    if di is None or ai is None: raise ValueError('Spalten für Datum und Betrag fehlen')
    result = []
    for line, row in enumerate(rows[1:], 2):
        if not any(x.strip() for x in row): continue
        try:
            booked = finance_date(row[di]); cents = finance_amount(row[ai]); desc = row[ti].strip() if ti is not None and ti < len(row) else 'CSV-Umsatz'
        except (IndexError, ValueError) as exc: raise ValueError(f'Zeile {line}: {exc}')
        low = desc.lower()
        internal = any(word in low for word in ('übertrag', 'uebertrag', 'transfer', 'kartenabrechnung', 'kreditkarte', 'card payment', 'visa payment'))
        kind = 'transfer' if internal else ('expense' if cents < 0 or account_kind == 'creditcard' else 'income')
        if account_kind == 'creditcard' and not internal: cents = -abs(cents)
        fp = hashlib.sha256(f'{booked}|{cents}|{desc.lower()}'.encode()).hexdigest()
        result.append({'booked_on': booked, 'description': desc, 'amount_cents': cents, 'kind': kind, 'fingerprint': fp, 'review_status': 'unclear' if internal else ('suggested' if kind == 'expense' else 'clear')})
    return result

def load_local_env():
    p = hermes_home() / '.env'
    if not p.exists():
        return
    for line in p.read_text(errors='replace').splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            k, v = line.split('=', 1)
            if k and k.isidentifier() and k not in os.environ:
                os.environ[k] = v.strip().strip('"').strip("'")

load_local_env()

def odds_status():
    key = os.environ.get('THE_ODDS_API_KEY', '').strip()
    if not key:
        return {'status':'unavailable','reason':'THE_ODDS_API_KEY nicht konfiguriert'}
    try:
        req = urllib.request.Request('https://api.the-odds-api.com/v4/sports/?apiKey=' + key, headers={'User-Agent':'Dashboard/1.0'})
        with urllib.request.urlopen(req, timeout=10) as r:
            return {'status':'available','http_status':r.status,'source':'The Odds API','requests_remaining':r.headers.get('x-requests-remaining','UNAVAILABLE'),'requests_used':r.headers.get('x-requests-used','UNAVAILABLE')}
    except Exception as exc:
        return {'status':'unavailable','source':'The Odds API','reason':type(exc).__name__}

SPORTS = {
    'NFL': 'americanfootball_nfl',
    'Bundesliga': 'soccer_germany_bundesliga',
    '2. Bundesliga': 'soccer_germany_bundesliga2',
    '3. Liga': 'soccer_germany_liga3',
}
BETTING_DB = Path(os.environ.get('BETTING_DB', str(Path.home() / '.local' / 'state' / 'hermes-betting' / 'betting.sqlite3'))).expanduser()
BETTING_SNAPSHOT_MAX_AGE = 1800
PAPER_BANKROLL_CENTS = 50000
PAPER_STAKE_CENTS = 250
MIN_FORECAST_SAMPLE = 20
ODDS_CACHE = {'at': 0.0, 'data': None} 
ODDS_CACHE_TTL = 120.0

def _betting_connect():
    BETTING_DB.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(BETTING_DB)
    con.row_factory = sqlite3.Row
    con.executescript('''
    CREATE TABLE IF NOT EXISTS odds_snapshots (
        id INTEGER PRIMARY KEY,
        retrieved_at TEXT NOT NULL,
        source TEXT NOT NULL,
        status TEXT NOT NULL,
        event_count INTEGER NOT NULL,
        payload TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS paper_bets (
        id INTEGER PRIMARY KEY,
        snapshot_id INTEGER NOT NULL,
        event_id TEXT NOT NULL,
        market TEXT NOT NULL,
        selection TEXT NOT NULL,
        bookmaker TEXT NOT NULL,
        odds REAL NOT NULL,
        model_probability REAL NOT NULL,
        expected_value REAL NOT NULL,
        stake_cents INTEGER NOT NULL,
        outcome TEXT NOT NULL DEFAULT 'open',
        created_at TEXT NOT NULL,
        settled_at TEXT,
        UNIQUE(snapshot_id, event_id, market, selection, bookmaker)
    );
    CREATE TABLE IF NOT EXISTS feature_snapshots (
        id INTEGER PRIMARY KEY,
        snapshot_id INTEGER NOT NULL,
        event_id TEXT NOT NULL,
        sport TEXT NOT NULL,
        retrieved_at TEXT NOT NULL,
        status TEXT NOT NULL,
        completeness REAL NOT NULL,
        payload TEXT NOT NULL,
        source_hashes TEXT NOT NULL,
        UNIQUE(snapshot_id, event_id, sport)
    );
    CREATE TABLE IF NOT EXISTS model_runs (
        id INTEGER PRIMARY KEY,
        snapshot_id INTEGER NOT NULL,
        event_id TEXT NOT NULL,
        sport TEXT NOT NULL,
        model_version TEXT NOT NULL,
        validation_status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        payload TEXT NOT NULL,
        UNIQUE(snapshot_id, event_id, sport, model_version)
    );
    CREATE TABLE IF NOT EXISTS forecasts (
        id INTEGER PRIMARY KEY,
        snapshot_id INTEGER NOT NULL,
        event_id TEXT NOT NULL,
        sport TEXT NOT NULL,
        market TEXT NOT NULL,
        selection TEXT NOT NULL,
        model_version TEXT NOT NULL,
        validation_status TEXT NOT NULL,
        probability REAL,
        fair_quote REAL,
        market_price REAL,
        uncertainty REAL,
        expected_value REAL,
        status TEXT NOT NULL,
        reason TEXT NOT NULL,
        created_at TEXT NOT NULL,
        payload TEXT NOT NULL,
        UNIQUE(snapshot_id, event_id, market, selection, model_version)
    );
    CREATE INDEX IF NOT EXISTS forecasts_event_idx ON forecasts(event_id, created_at);
    ''')
    return con

def save_betting_snapshot(feed):
    if not isinstance(feed, dict) or feed.get('status') != 'available':
        raise ValueError('Nur verfügbare Quoten-Snapshots werden gespeichert')
    retrieved_at = str(feed.get('retrieved_at') or datetime.now().astimezone().isoformat(timespec='seconds'))
    payload = json.dumps(feed, ensure_ascii=False, separators=(',', ':'))
    with closing(_betting_connect()) as con:
        cursor = con.execute('INSERT INTO odds_snapshots(retrieved_at, source, status, event_count, payload) VALUES (?, ?, ?, ?, ?)',
                             (retrieved_at, str(feed.get('source') or 'UNAVAILABLE'), 'available', len(feed.get('events', [])), payload))
        con.commit()
        return {'id': cursor.lastrowid, 'retrieved_at': retrieved_at, 'event_count': len(feed.get('events', []))}

def latest_betting_snapshot():
    if not BETTING_DB.is_file():
        return None
    try:
        with closing(sqlite3.connect(f'file:{BETTING_DB}?mode=ro', uri=True)) as con:
            row = con.execute('SELECT id, retrieved_at, source, event_count, payload FROM odds_snapshots ORDER BY id DESC LIMIT 1').fetchone()
    except sqlite3.Error:
        return None
    if not row:
        return None
    try:
        payload = json.loads(row[4])
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    payload.update(id=row[0], snapshot_id=row[0], snapshot_at=row[1], snapshot_source=row[2], snapshot_event_count=row[3])
    return payload

def save_feature_snapshot(snapshot_id, event_id, sport, snapshot):
    if not isinstance(snapshot, dict):
        raise ValueError('Feature-Snapshot ungültig')
    retrieved_at = str(snapshot.get('retrieved_at') or datetime.now().astimezone().isoformat(timespec='seconds'))
    source_hashes = [item.get('payload_hash') for item in snapshot.get('sources', []) if item.get('payload_hash')]
    payload = json.dumps(snapshot, ensure_ascii=False, separators=(',', ':'))
    with closing(_betting_connect()) as con:
        con.execute('''INSERT OR IGNORE INTO feature_snapshots
            (snapshot_id, event_id, sport, retrieved_at, status, completeness, payload, source_hashes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
                    (int(snapshot_id), str(event_id)[:160], str(sport)[:40], retrieved_at,
                     str(snapshot.get('status') or 'unavailable'), float(snapshot.get('completeness') or 0),
                     payload, json.dumps(source_hashes, separators=(',', ':'))))
        row = con.execute('SELECT id FROM feature_snapshots WHERE snapshot_id = ? AND event_id = ? AND sport = ?',
                          (int(snapshot_id), str(event_id)[:160], str(sport)[:40])).fetchone()
        con.commit()
    return {'id': row[0], 'snapshot_id': int(snapshot_id), 'event_id': str(event_id), 'sport': str(sport)}

def save_model_run(snapshot_id, event_id, sport, model):
    if not isinstance(model, dict) or not model.get('model_version'):
        raise ValueError('Model-Lauf ungültig')
    model_version = str(model['model_version'])[:80]
    created_at = datetime.now().astimezone().isoformat(timespec='seconds')
    payload = json.dumps(model, ensure_ascii=False, separators=(',', ':'))
    with closing(_betting_connect()) as con:
        con.execute('''INSERT OR IGNORE INTO model_runs
            (snapshot_id, event_id, sport, model_version, validation_status, created_at, payload)
            VALUES (?, ?, ?, ?, ?, ?, ?)''',
                    (int(snapshot_id), str(event_id)[:160], str(sport)[:40], model_version,
                     str(model.get('validation_status') or 'unvalidated')[:40], created_at, payload))
        row = con.execute('''SELECT id, model_version, validation_status FROM model_runs
                             WHERE snapshot_id = ? AND event_id = ? AND sport = ? AND model_version = ?''',
                          (int(snapshot_id), str(event_id)[:160], str(sport)[:40], model_version)).fetchone()
        con.commit()
    return {'id': row[0], 'model_version': row[1], 'validation_status': row[2]}

def save_forecast(snapshot_id, event_id, sport, market, selection, forecast):
    if not isinstance(forecast, dict) or not forecast.get('model_version'):
        raise ValueError('Forecast ungültig')
    model_version = str(forecast['model_version'])[:80]
    created_at = datetime.now().astimezone().isoformat(timespec='seconds')
    payload = json.dumps(forecast, ensure_ascii=False, separators=(',', ':'))
    values = (int(snapshot_id), str(event_id)[:160], str(sport)[:40], str(market)[:40], str(selection)[:160], model_version,
              str(forecast.get('validation_status') or 'unvalidated')[:40], forecast.get('model_probability'), forecast.get('fair_quote'),
              forecast.get('market_price'), forecast.get('uncertainty'), forecast.get('expected_value'),
              str(forecast.get('status') or 'NO_CALL')[:24], str(forecast.get('reason') or '')[:240], created_at, payload)
    with closing(_betting_connect()) as con:
        con.execute('''INSERT OR IGNORE INTO forecasts
            (snapshot_id, event_id, sport, market, selection, model_version, validation_status,
             probability, fair_quote, market_price, uncertainty, expected_value, status, reason, created_at, payload)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''', values)
        row = con.execute('''SELECT id FROM forecasts WHERE snapshot_id = ? AND event_id = ? AND market = ?
                             AND selection = ? AND model_version = ?''',
                          (values[0], values[1], values[3], values[4], values[5])).fetchone()
        con.commit()
    return {'id': row[0], 'snapshot_id': int(snapshot_id), 'event_id': str(event_id), 'market': str(market), 'selection': str(selection), 'model_version': model_version}

def latest_forecasts(snapshot_id=None):
    if not BETTING_DB.is_file():
        return []
    query = '''SELECT id, snapshot_id, event_id, sport, market, selection, model_version,
                      validation_status, probability, fair_quote, market_price, uncertainty,
                      expected_value, status, reason, created_at, payload
               FROM forecasts'''
    args = ()
    if snapshot_id is not None:
        query += ' WHERE snapshot_id = ?'
        args = (int(snapshot_id),)
    query += ' ORDER BY id DESC LIMIT 500'
    try:
        with closing(sqlite3.connect(f'file:{BETTING_DB}?mode=ro', uri=True)) as con:
            rows = con.execute(query, args).fetchall()
    except sqlite3.Error:
        return []
    result = []
    for row in rows:
        try:
            payload = json.loads(row[16])
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {}
        payload.update({
            'id': row[0], 'snapshot_id': row[1], 'event_id': row[2], 'sport': row[3],
            'market': row[4], 'selection': row[5], 'model_version': row[6],
            'validation_status': row[7], 'model_probability': row[8], 'fair_quote': row[9],
            'market_price': row[10], 'uncertainty': row[11], 'expected_value': row[12],
            'status': row[13], 'reason': row[14], 'created_at': row[15],
        })
        result.append(payload)
    return result

def forecast_metrics():
    bets = [bet for bet in _paper_bet_rows() if bet['outcome'] in ('win', 'loss')]
    if len(bets) < MIN_FORECAST_SAMPLE:
        return {
            'status': 'UNAVAILABLE', 'sample_size': len(bets),
            'reason': f'Mindestens {MIN_FORECAST_SAMPLE} abgeschlossene Paper-Wetten erforderlich',
            'brier_score': None, 'log_loss': None, 'roi': None,
            'calibration': 'UNAVAILABLE', 'closing_line_value': 'UNAVAILABLE',
        }
    brier = 0.0
    log_loss = 0.0
    profit = 0
    stake_total = 0
    for bet in bets:
        probability = min(1 - 1e-9, max(1e-9, float(bet['model_probability'])))
        target = 1 if bet['outcome'] == 'win' else 0
        brier += (probability - target) ** 2
        log_loss -= target * math.log(probability) + (1 - target) * math.log(1 - probability)
        stake = int(bet['stake_cents'])
        stake_total += stake
        profit += round(stake * (float(bet['odds']) - 1)) if target else -stake
    return {
        'status': 'AVAILABLE', 'sample_size': len(bets),
        'reason': None, 'brier_score': round(brier / len(bets), 6),
        'log_loss': round(log_loss / len(bets), 6),
        'roi': round(profit / stake_total, 6) if stake_total else None,
        'calibration': 'UNAVAILABLE', 'closing_line_value': 'UNAVAILABLE',
    }

def _snapshot_age(retrieved_at):
    try:
        parsed = datetime.fromisoformat(retrieved_at)
        if parsed.tzinfo is None:
            parsed = parsed.astimezone()
        return max(0, int((datetime.now(parsed.tzinfo) - parsed).total_seconds()))
    except (TypeError, ValueError):
        return None

def _with_snapshot_state(feed):
    snapshot_at = feed.get('snapshot_at')
    age = _snapshot_age(snapshot_at)
    feed['snapshot_age_seconds'] = age
    feed['freshness'] = 'fresh' if age is not None and age <= BETTING_SNAPSHOT_MAX_AGE else 'stale'
    return feed

def fetch_odds_feed():
    import time
    if ODDS_CACHE['data'] is not None and time.time() - ODDS_CACHE['at'] < ODDS_CACHE_TTL:
        return ODDS_CACHE['data']
    key = os.environ.get('THE_ODDS_API_KEY', '').strip()
    if not key:
        return {'status':'unavailable','source':'The Odds API','events':[], 'reason':'THE_ODDS_API_KEY nicht konfiguriert'}
    events = []
    for league, sport_key in SPORTS.items():
        query = urlencode({'apiKey':key, 'regions':'eu', 'markets':'h2h,spreads,totals', 'oddsFormat':'decimal'})
        try:
            with urllib.request.urlopen(urllib.request.Request('https://api.the-odds-api.com/v4/sports/' + sport_key + '/odds/?' + query, headers={'User-Agent':'Dashboard/1.0'}), timeout=10) as r:
                payload = json.loads(r.read())
            for event in payload if isinstance(payload, list) else []:
                events.append(normalize_event(event, league))
        except Exception as exc:
            if not events:
                return {'status':'unavailable','source':'The Odds API','events':[], 'reason':type(exc).__name__}
    result = {'status':'available','source':'The Odds API','retrieved_at':__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(), 'events':events}
    ODDS_CACHE.update(at=__import__('time').time(), data=result)
    return result

def refresh_betting_snapshot():
    feed = fetch_odds_feed()
    if feed.get('status') != 'available':
        return feed
    saved = save_betting_snapshot(feed)
    events = []
    source_statuses = []
    forecast_errors = []
    candidate_count = 0
    for original in feed.get('events', []):
        event = dict(original)
        sport = 'nfl' if event.get('competition') == 'NFL' else 'football'
        try:
            features = betting_sources.source_snapshot(event, sport, datetime.now().astimezone())
            source_statuses.append(features.get('status'))
            forecast_errors.extend(str(error) for error in features.get('errors', []))
            save_feature_snapshot(saved['id'], event.get('id'), sport, features)
            model = betting_forecast.forecast_nfl(event, features) if sport == 'nfl' else betting_forecast.forecast_football(event, features)
            model = {**model, 'sport': sport, 'source_status': features.get('status')}
            save_model_run(saved['id'], event.get('id'), sport, model)
            forecasts = []
            for market in event.get('markets', []):
                if market.get('market') != 'h2h':
                    continue
                offers = {}
                for outcome in market.get('outcomes', []):
                    name = outcome.get('name')
                    try:
                        price = float(outcome.get('price'))
                    except (TypeError, ValueError):
                        continue
                    if name and price > 1 and (name not in offers or price > offers[name][0]):
                        offers[name] = (price, market.get('bookmaker') or 'UNAVAILABLE')
                for selection, (price, bookmaker) in offers.items():
                    probability = (model.get('probabilities') or {}).get(selection)
                    if probability is None:
                        continue
                    candidate = {
                        **model, 'market': 'h2h', 'selection': selection,
                        'model_probability': probability,
                        'fair_quote': (model.get('fair_quotes') or {}).get(selection),
                    }
                    gated = betting_forecast.gate_forecast(candidate, price)
                    gated.update(bookmaker=bookmaker, best_bookmaker=bookmaker)
                    save_forecast(saved['id'], event.get('id'), sport, 'h2h', selection, gated)
                    forecasts.append(gated)
            if not forecasts:
                fallback = {
                    **model, 'market': 'h2h', 'selection': 'UNAVAILABLE',
                    'model_probability': model.get('model_probability'),
                    'fair_quote': model.get('fair_quote'), 'market_price': None,
                    'expected_value': None, 'status': 'NO_CALL',
                    'reason': model.get('reason') or (features.get('errors') or ['Keine modellierte Auswahl'])[0],
                }
                save_forecast(saved['id'], event.get('id'), sport, 'h2h', 'UNAVAILABLE', fallback)
                forecasts.append(fallback)
            paper = [item for item in forecasts if item.get('status') == 'PAPER']
            best = max(paper or forecasts, key=lambda item: float(item.get('expected_value') or -999), default=None)
            event['forecasts'] = forecasts
            if best:
                event.update(best)
                event['tip_text'] = (f"PAPER · {best['selection']} · eigene Chance {best['model_probability']:.1%} · "
                                     f"Modellquote {best['fair_quote']:.2f} · Markt {best['market_price']:.2f}") if best.get('status') == 'PAPER' else f"NO_CALL · {best.get('reason', 'keine Prognose')}"
                if best.get('status') == 'PAPER':
                    candidate_count += 1
            else:
                event.update({'status': 'NO_CALL', 'reason': 'Keine modellierte h2h-Auswahl', 'rationale': 'NO_CALL · keine unabhängige Auswahl verfügbar'})
        except Exception as exc:
            source_statuses.append('unavailable')
            forecast_errors.append(f"{event.get('id', 'UNAVAILABLE')}: {type(exc).__name__}")
            event.update({'status': 'NO_CALL', 'reason': f'Forecast fehlgeschlagen: {type(exc).__name__}', 'rationale': 'NO_CALL · Forecast fehlgeschlagen'})
        events.append(event)
    status = 'available' if source_statuses and all(item == 'available' for item in source_statuses) else 'partial' if 'available' in source_statuses else 'unavailable'
    return {**feed, 'events': events, 'snapshot_id': saved['id'], 'snapshot_at': saved['retrieved_at'],
            'snapshot_event_count': saved['event_count'], 'freshness': 'fresh', 'snapshot_age_seconds': 0,
            'forecast_status': status, 'forecast_candidates': candidate_count, 'forecast_errors': forecast_errors}

def _with_forecast_state(feed):
    if not isinstance(feed, dict):
        return feed
    rows = latest_forecasts(feed.get('snapshot_id'))
    by_event = defaultdict(list)
    for row in rows:
        by_event[row['event_id']].append(row)
    source_statuses = [row.get('source_status') for row in rows]
    feed['forecast_status'] = 'available' if 'available' in source_statuses else 'partial' if 'partial' in source_statuses else 'unavailable' if rows else 'UNAVAILABLE'
    feed['forecast_errors'] = [row.get('reason') for row in rows if row.get('status') == 'NO_CALL' and row.get('reason')]
    for event in feed.get('events', []):
        forecasts = by_event.get(event.get('id'), [])
        paper = [item for item in forecasts if item.get('status') == 'PAPER']
        best = max(paper or forecasts, key=lambda item: float(item.get('expected_value') or -999), default=None)
        event['forecasts'] = forecasts
        if best:
            event.update(best)
            if best.get('status') != 'PAPER':
                event['tip_text'] = f"NO_CALL · {best.get('reason', 'keine unabhängige Prognose')}"
        else:
            event.update({'status': 'NO_CALL', 'reason': 'Keine unabhängige Prognose gespeichert', 'rationale': 'NO_CALL · keine unabhängige Prognose gespeichert'})
    return feed

def odds_feed():
    if not os.environ.get('THE_ODDS_API_KEY', '').strip():
        return {'status': 'unavailable', 'source': 'The Odds API', 'events': [], 'reason': 'THE_ODDS_API_KEY nicht konfiguriert'}
    snapshot = latest_betting_snapshot()
    if snapshot is None:
        return {'status': 'unavailable', 'source': 'The Odds API', 'events': [], 'reason': 'Kein Quoten-Snapshot vorhanden; Snapshot-Timer ausführen'}
    return _with_snapshot_state(snapshot)

def _paper_bet_rows():
    if not BETTING_DB.is_file():
        return []
    try:
        with closing(sqlite3.connect(f'file:{BETTING_DB}?mode=ro', uri=True)) as con:
            con.row_factory = sqlite3.Row
            return [dict(row) for row in con.execute('SELECT * FROM paper_bets ORDER BY id DESC LIMIT 200')]
    except sqlite3.Error:
        return []

def betting_state():
    feed = _with_forecast_state(odds_feed())
    bets = _paper_bet_rows()
    settled = [bet for bet in bets if bet['outcome'] in ('win', 'loss', 'void')]
    profit_cents = 0
    settled_stake = 0
    brier_total = 0.0
    for bet in settled:
        stake = int(bet['stake_cents'])
        settled_stake += stake
        probability = float(bet['model_probability'])
        target = 1 if bet['outcome'] == 'win' else 0
        brier_total += (probability - target) ** 2
        if bet['outcome'] == 'win':
            profit_cents += round(stake * (float(bet['odds']) - 1))
        elif bet['outcome'] == 'loss':
            profit_cents -= stake
    metrics = {
        'bankroll_cents': PAPER_BANKROLL_CENTS,
        'open_bets': sum(bet['outcome'] == 'open' for bet in bets),
        'settled_bets': len(settled),
        'profit_cents': profit_cents,
        'roi': round(profit_cents / settled_stake, 4) if settled_stake else None,
        'brier_score': round(brier_total / len(settled), 4) if settled else None,
        'closing_line_value': 'UNAVAILABLE · Schlussquoten noch nicht gespeichert',
    }
    candidates = [event for event in feed.get('events', []) if event.get('status') == 'PAPER']
    return {'feed': feed, 'candidates': candidates, 'paper_bets': bets, 'metrics': metrics}

def save_paper_bet(payload):
    if not isinstance(payload, dict):
        raise ValueError('Ungültige Paper-Wette')
    required = ('snapshot_id', 'event_id', 'market', 'selection', 'bookmaker', 'odds', 'model_probability', 'expected_value')
    if any(not payload.get(key) and payload.get(key) != 0 for key in required):
        raise ValueError('Paper-Wette unvollständig')
    try:
        snapshot_id = int(payload['snapshot_id']); odds = float(payload['odds']); probability = float(payload['model_probability']); expected_value = float(payload['expected_value'])
    except (TypeError, ValueError):
        raise ValueError('Paper-Wette enthält ungültige Zahlen')
    if snapshot_id <= 0 or odds <= 1 or not 0 < probability < 1 or expected_value <= 0:
        raise ValueError('Paper-Wette außerhalb sicherer Grenzen')
    now = datetime.now().astimezone().isoformat(timespec='seconds')
    try:
        with closing(_betting_connect()) as con:
            cursor = con.execute('''INSERT INTO paper_bets(snapshot_id, event_id, market, selection, bookmaker, odds,
                model_probability, expected_value, stake_cents, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (snapshot_id, str(payload['event_id'])[:160], str(payload['market'])[:40], str(payload['selection'])[:160],
                 str(payload['bookmaker'])[:120], odds, probability, expected_value, PAPER_STAKE_CENTS, now))
            con.commit()
            return {'id': cursor.lastrowid, 'status': 'open', 'stake_cents': PAPER_STAKE_CENTS}
    except sqlite3.IntegrityError:
        raise ValueError('Paper-Wette für diesen Snapshot bereits gespeichert')

def settle_paper_bet(bet_id, outcome):
    if outcome not in ('win', 'loss', 'void'):
        raise ValueError('Ergebnis muss win, loss oder void sein')
    try:
        bet_id = int(bet_id)
    except (TypeError, ValueError):
        raise ValueError('Ungültige Paper-Wetten-ID')
    with closing(_betting_connect()) as con:
        cursor = con.execute("UPDATE paper_bets SET outcome = ?, settled_at = ? WHERE id = ? AND outcome = 'open'",
                             (outcome, datetime.now().astimezone().isoformat(timespec='seconds'), bet_id))
        con.commit()
        if cursor.rowcount != 1:
            raise ValueError('Paper-Wette nicht offen oder nicht gefunden')
    return {'id': bet_id, 'outcome': outcome}

def normalize_event(event, league):
    markets = []
    for bookmaker in event.get('bookmakers', []):
        for market in bookmaker.get('markets', []):
            markets.append({'bookmaker':bookmaker.get('title','UNAVAILABLE'), 'market':market.get('key','UNAVAILABLE'), 'outcomes':market.get('outcomes',[])})
    result = {'id':event.get('id','UNAVAILABLE'), 'sport':event.get('sport_key','UNAVAILABLE'), 'competition':league,
            'home':event.get('home_team','UNAVAILABLE'), 'away':event.get('away_team','UNAVAILABLE'),
            'start':event.get('commence_time','UNAVAILABLE'), 'markets':markets}
    return result

def baseline_model(event):
    """Find paper-only line-shopping candidates; this is not an independent model."""
    groups = defaultdict(list)
    for item in event.get('markets', []):
        market = item.get('market')
        if market not in ('h2h', 'spreads', 'totals') or not item.get('bookmaker'):
            continue
        outcomes = [o for o in item.get('outcomes', [])
                    if isinstance(o.get('name'), str) and isinstance(o.get('price'), (int, float))
                    and o['price'] > 1]
        if len(outcomes) < 2:
            continue
        point = tuple(sorted(str(o.get('point')) for o in outcomes)) if market != 'h2h' else ()
        overround = sum(1 / float(o['price']) for o in outcomes)
        for outcome in outcomes:
            groups[(market, point, outcome['name'])].append(
                (item['bookmaker'], 1 / float(outcome['price']) / overround,
                 float(outcome['price'])))
    candidates = []
    for (market, point, name), rows in groups.items():
        if len({row[0] for row in rows}) < 3:
            continue
        for bookmaker, _probability, price in rows:
            reference = [row[1] for row in rows if row[0] != bookmaker]
            if len(reference) < 2:
                continue
            probability = sum(reference) / len(reference)
            spread = (sum((p - probability) ** 2 for p in reference) / len(reference)) ** 0.5
            fair_quote = 1 / probability
            expected_value = price * probability - 1
            edge = price - fair_quote
            candidates.append((expected_value, {'market': market, 'selection': name,
                'model_probability': round(probability, 6), 'fair_quote': round(fair_quote, 4),
                'market_price': round(price, 4), 'edge': round(edge, 4),
                'edge_percent': round(edge / fair_quote * 100, 2),
                'expected_value': round(expected_value, 4),
                'best_bookmaker': bookmaker,
                'confidence': round(max(0.0, min(1.0, 1 - spread * 4)), 3),
                'rationale': 'PAPER ONLY · marktbasierte Referenz ohne unabhängiges Vorhersagemodell.',
                'paper_stake_cents': PAPER_STAKE_CENTS if expected_value >= 0.02 else 0,
                'status': 'PAPER' if expected_value >= 0.02 and spread <= 0.08 else 'NO_CALL'}))
    if not candidates:
        return {'tip_text': 'NO_CALL · mindestens 3 unabhängige Buchmacher nötig', 'selection': None,
                'market': None, 'model_probability': None, 'fair_quote': None, 'market_price': None,
                'edge': None, 'edge_percent': None, 'expected_value': None, 'best_bookmaker': None,
                'confidence': 0, 'paper_stake_cents': 0, 'rationale': 'Keine verifizierte unabhängige Marktgruppe.',
                'status': 'NO_CALL'}
    _, model = max(candidates, key=lambda item: item[0])
    if model['status'] != 'PAPER':
        model['rationale'] = 'NO_CALL · kein ausreichend stabiler Paper-Edge; unabhängiges Modell fehlt.'
    model['tip_text'] = f"{model['status']} · {model['selection']} · {model['market']} · Fair {model['fair_quote']:.2f} · Markt {model['market_price']:.2f}"
    return model

def router_stats():
    # Read real usage from Nine Router's local SQLite database. Never estimate.
    endpoint = 'http://127.0.0.1:20128/v1'
    try:
        with socket.create_connection(('127.0.0.1', 20128), timeout=2):
            health = 'online'
    except OSError as exc:
        return {'status':'offline','health':'offline','source':'Nine Router','endpoint':endpoint,'usage':'UNAVAILABLE','reason':type(exc).__name__}
    db = Path(os.environ.get('NINEROUTER_USAGE_DB', str(Path.home() / '.9router' / 'db' / 'data.sqlite'))).expanduser()
    try:
        with closing(sqlite3.connect(f'file:{db}?mode=ro', uri=True, timeout=1)) as con:
            row = con.execute('SELECT COALESCE(SUM(promptTokens),0), COALESCE(SUM(completionTokens),0), COALESCE(SUM(cost),0), COUNT(*) FROM usageHistory WHERE status = ?', ('ok',)).fetchone()
            cached = 0
            for (raw,) in con.execute('SELECT tokens FROM usageHistory WHERE status = ? AND tokens IS NOT NULL', ('ok',)):
                try: cached += int(json.loads(raw).get('cached_tokens', 0) or 0)
                except (TypeError, ValueError, json.JSONDecodeError): pass
            daily = [json.loads(raw) for (raw,) in con.execute('SELECT data FROM usageDaily ORDER BY dateKey DESC LIMIT 30')]
        prompt, completion, cost, requests = row
        total = int(prompt + completion)
        return {'status':'available','health':health,'source':'Nine Router','endpoint':endpoint,'usage':'AVAILABLE','data':{'prompt_tokens':int(prompt),'completion_tokens':int(completion),'cached_tokens':cached,'total_tokens':total,'requests':requests,'cost':round(float(cost), 4),'savings':'UNAVAILABLE','daily':daily},'reason':'Direkt aus Nine Router usageHistory/usageDaily'}
    except Exception as exc:
        return {'status':'online','health':health,'source':'Nine Router','endpoint':endpoint,'usage':'UNAVAILABLE','reason':f'Usage-Datenbank nicht lesbar: {type(exc).__name__}'}


def _port_open(port):
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=1): return True
    except OSError: return False

def agent_stats():
    """Read-only local profile/gateway inventory; no secrets or task payloads."""
    profiles_root = hermes_home() / 'profiles'
    names = ['default']
    if profiles_root.is_dir():
        names += sorted(p.name for p in profiles_root.iterdir() if p.is_dir() and not p.name.startswith('.') and p.name != 'default')
    agents = []
    for name in names:
        unit = f'hermes-gateway-{name}.service' if name != 'default' else 'hermes-gateway.service'
        try:
            state = subprocess.check_output(['systemctl','--user','is-active',unit], text=True, stderr=subprocess.DEVNULL, timeout=1).strip()
        except Exception:
            state = 'stopped'
        agents.append({'name': name, 'state': state, 'unit': unit})
    return {'source':'lokale Hermes-Profile und systemd-user units', 'profiles':len(agents), 'active':sum(a['state']=='active' for a in agents), 'agents':agents}

def telemetry():
    """Read-only snapshot from Hermes Kanban DB; omit unverifiable details."""
    db = hermes_home() / 'kanban.db'
    result = {'status': 'UNAVAILABLE', 'source': str(db), 'agents': [], 'tasks': {}, 'projects': {}}
    try:
        with closing(sqlite3.connect(f'file:{db}?mode=ro', uri=True, timeout=1)) as con:
            rows = con.execute('SELECT assignee, status, COUNT(*) FROM tasks GROUP BY assignee, status').fetchall()
            task_counts = con.execute('SELECT status, COUNT(*) FROM tasks GROUP BY status').fetchall()
            project_counts = con.execute("SELECT COALESCE(project_id, 'UNAVAILABLE'), COUNT(*) FROM tasks GROUP BY project_id").fetchall()
        agents = {}
        for assignee, status, count in rows:
            name = assignee or 'UNAVAILABLE'
            agents.setdefault(name, {})[status or 'UNAVAILABLE'] = count
        task_counts = [(status or 'UNAVAILABLE', count) for status, count in task_counts]
        result.update(status='AVAILABLE', agents=[{'name': name, 'tasks': counts} for name, counts in sorted(agents.items())],
                      tasks=dict(task_counts), projects=dict(project_counts))
    except (OSError, sqlite3.Error):
        pass
    return result


def engineering_status():
    """Bounded, read-only status summary; never exposes command output or secrets."""
    retrieved_at = datetime.now().astimezone().isoformat(timespec='seconds')
    def source(name, status, value, origin, error=None):
        return {'name': name, 'status': status, 'value': value, 'source': origin, 'error': error}
    try:
        gateway = subprocess.check_output(['systemctl', '--user', 'is-active', 'hermes-gateway.service'], text=True, stderr=subprocess.DEVNULL, timeout=1).strip()
    except Exception:
        gateway = 'stopped'
    gateway_ok = gateway == 'active'
    router_ok = _port_open(20128)
    items = [
        source('Hermes gateway', 'AVAILABLE' if gateway_ok else 'UNAVAILABLE', gateway if gateway_ok else None, 'systemd user unit', None if gateway_ok else 'gateway stopped or unsupported'),
        source('Nine Router', 'AVAILABLE' if router_ok else 'UNAVAILABLE', 'online' if router_ok else None, '127.0.0.1:20128', None if router_ok else 'router unavailable'),
        source('Dashboard', 'AVAILABLE', 'online', 'local dashboard server'),
    ]
    return {'overall': 'AVAILABLE' if gateway_ok and router_ok else 'UNAVAILABLE', 'retrieved_at': retrieved_at, 'sources': items}

CONVERSATION_MAX_INPUT = 4000
HERMES_RUN_TIMEOUT = 10
HERMES_TASK_OWNERS = {}
HERMES_TASK_OWNERS_LIMIT = 256

def _dashboard_csrf(handler):
    return next((part.split('=', 1)[1] for part in handler.headers.get('Cookie', '').split('; ') if part.startswith('dashboard_csrf=')), '')

def _remember_hermes_task(task_id, csrf):
    if len(HERMES_TASK_OWNERS) >= HERMES_TASK_OWNERS_LIMIT:
        del HERMES_TASK_OWNERS[next(iter(HERMES_TASK_OWNERS))]
    HERMES_TASK_OWNERS[task_id] = csrf

def _task_owned(handler, task_id):
    csrf = _dashboard_csrf(handler)
    return bool(csrf and HERMES_TASK_OWNERS.get(task_id) == csrf)

def _hermes_executable():
    configured = os.environ.get('HERMES_EXECUTABLE', '').strip()
    candidates = [configured, shutil.which('hermes'), str(hermes_home() / 'hermes-agent' / 'hermes')]
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return str(Path(candidate).resolve())
    return None

def _native_task_cli(args):
    executable = _hermes_executable()
    if not executable:
        return None, 'Hermes executable unavailable'
    env = os.environ.copy()
    home = hermes_home()
    # Native CLI expects root HERMES_HOME plus explicit profile routing.
    if home.parent.name == 'profiles':
        root, profile = home.parent.parent, home.name
    else:
        root = home
        profile = env.get('HERMES_PROFILE', '').strip() or 'coder'
    env['HERMES_HOME'] = str(root)
    env['HERMES_PROFILE'] = profile
    try:
        completed = subprocess.run([executable, 'kanban', *args, '--json'], env=env,
                                   capture_output=True, text=True, timeout=HERMES_RUN_TIMEOUT,
                                   check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, type(exc).__name__
    if completed.returncode:
        detail = (completed.stderr or completed.stdout).strip().splitlines()
        suffix = f': {detail[-1][:200]}' if detail else ''
        return None, f'Hermes CLI failed ({completed.returncode}){suffix}'
    try:
        return json.loads(completed.stdout), None
    except json.JSONDecodeError:
        return None, 'Hermes CLI response invalid'

def _task_payload(task_id, current):
    task = current.get('task', current) if isinstance(current, dict) else {}
    runs = current.get('runs', []) if isinstance(current, dict) else []
    run = next((item for item in runs if isinstance(item, dict) and item.get('status') in ('running', 'done', 'failed', 'blocked')), None)
    if run is None:
        run = next((item for item in runs if isinstance(item, dict)), None)
    metadata = run.get('metadata') if isinstance(run, dict) else None
    metadata = metadata if isinstance(metadata, dict) else {}
    return {'status': task.get('status', 'unknown'), 'task_id': task_id,
            'run_id': task.get('current_run_id') or (run or {}).get('id'),
            'session_id': metadata.get('worker_session_id'), 'result': task.get('result'),
            'latest_summary': current.get('latest_summary', task.get('latest_summary'))}

def hermes_run(message):
    """Create one native Hermes task, then return its real state."""
    if not isinstance(message, str) or not message.strip() or len(message) > CONVERSATION_MAX_INPUT:
        raise ValueError('Ungültige Auftragseingabe')
    key = hashlib.sha256(message.encode()).hexdigest()
    created, error = _native_task_cli(['create', 'Dashboard order', '--body', message,
        '--assignee', 'coder', '--workspace', 'scratch', '--idempotency-key', f'dashboard-order:{key}'])
    if not created:
        return {'status': 'unavailable', 'error': 'Native Hermes task creation failed', 'reason': error}
    task_id = created.get('id') if isinstance(created, dict) else None
    if not task_id:
        return {'status': 'unavailable', 'error': 'Native Hermes response invalid', 'reason': 'task ID missing'}
    _native_task_cli(['dispatch'])
    deadline = time.monotonic() + HERMES_RUN_TIMEOUT
    current = None
    error = None
    while time.monotonic() < deadline:
        current, error = _native_task_cli(['show', str(task_id)])
        if not current:
            break
        status = current.get('task', current).get('status') if isinstance(current, dict) else None
        if status in ('done', 'completed', 'failed', 'blocked'):
            break
        time.sleep(0.2)
    if not current:
        return {'status': 'unavailable', 'error': 'Native Hermes task lookup failed', 'reason': error, 'task_id': task_id}
    return _task_payload(task_id, current)

def conversation(message):
    """Route text through local authenticated NineRouter without exposing credentials."""
    if not isinstance(message, str) or not message.strip():
        raise ValueError('Nachricht fehlt')
    if len(message) > CONVERSATION_MAX_INPUT:
        raise ValueError('Nachricht zu lang')
    key = os.environ.get('NINEROUTER_API_KEY', '').strip()
    base_url = 'http://127.0.0.1:20128/v1'
    config = hermes_home() / 'config.yaml'
    if not key and config.exists():
        for line in config.read_text(errors='replace').splitlines():
            if line.startswith('  api_key: '): key = line.split(':', 1)[1].strip().strip('"').strip("'")
            elif line.startswith('  base_url: '): base_url = line.split(':', 1)[1].strip().strip('"').strip("'")
    if not key:
        return {'error': 'Lokaler Hermes-Adapter nicht verfügbar', 'reason': 'Hermes-Provider nicht konfiguriert'}
    payload = json.dumps({'model': 'cx/gpt-5.6-luna', 'messages': [{'role': 'user', 'content': message}], 'stream': False}).encode()
    request = urllib.request.Request(base_url.rstrip('/') + '/chat/completions', data=payload,
        headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'}, method='POST')
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = json.loads(response.read())
        text = data.get('choices', [{}])[0].get('message', {}).get('content')
        if not isinstance(text, str) or not text.strip():
            return {'error': 'Keine verwertbare Modellantwort', 'reason': 'NineRouter response missing content'}
        return {'response': text.strip(), 'model': 'cx/gpt-5.6-luna'}
    except urllib.error.HTTPError as exc:
        return {'error': 'NineRouter-Anfrage fehlgeschlagen', 'reason': f'HTTP {exc.code}'}
    except (OSError, json.JSONDecodeError):
        return {'error': 'NineRouter nicht erreichbar', 'reason': 'lokaler Router nicht verfügbar'}
    except Exception:
        return {'error': 'NineRouter-Anfrage fehlgeschlagen', 'reason': 'unbekannter lokaler Adapterfehler'}


def systems():
    def run(cmd):
        try: return subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL, timeout=2).strip()
        except Exception: return 'UNAVAILABLE'
    def number(path, divisor=1):
        try: return round(int(Path(path).read_text().strip()) / divisor, 1)
        except Exception: return None
    mem = {}
    try:
        for line in Path('/proc/meminfo').read_text().splitlines():
            k, v = line.split(':', 1); mem[k] = int(v.split()[0]) * 1024
    except Exception: pass
    disk = shutil.disk_usage('/')
    try: temp = number('/sys/class/thermal/thermal_zone0/temp', 1000)
    except Exception: temp = None
    router = 'online' if _port_open(20128) else 'offline'
    return {'gateway':run(['systemctl','--user','is-active','hermes-gateway.service']),
            'router':router,
            'dashboard':run(['systemctl','--user','is-active','dashboard.service']),
            'cpu_load':run(['sh','-c',"awk '{print $1, $2, $3}' /proc/loadavg"]),
            'temperature':temp,
            'memory_available':mem.get('MemAvailable'),
            'memory_total':mem.get('MemTotal'),
            'disk_available':disk.free,
            'disk_total':disk.total,
            'disk_percent':round((disk.used / disk.total) * 100, 1)}

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlsplit(self.path)
        if parsed.path == '/':
            token = secrets.token_urlsafe(24)
            body = (ROOT / 'index.html').read_bytes()
            self.send_response(200)
            self.send_header('Set-Cookie', f'dashboard_csrf={token}; SameSite=Strict; Path=/')
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers(); self.wfile.write(body); return
        if parsed.path == '/finanzen.html': self.path = '/finanzen.html'
        if parsed.path.startswith('/api/finance/'):
            return self.finance_get(parsed.path)
        if parsed.path == '/api/systems': return self.send_json(systems())
        if parsed.path == '/api/engineering-status': return self.send_json(engineering_status())
        if parsed.path == '/api/agents': return self.send_json(agent_stats())
        if parsed.path == '/api/telemetry': return self.send_json(telemetry())
        if parsed.path == '/api/jarvis-config': return self.send_json(jarvis_config())
        if parsed.path == '/api/tokens': return self.send_json(router_stats())
        if parsed.path == '/api/odds/status': return self.send_json(odds_status())
        if parsed.path == '/api/betting/state': return self.send_json(betting_state())
        if parsed.path == '/api/trading/status': return self.send_json({'config': hyperliquid_bot.config(), 'strategy': hyperliquid_bot.strategy_contract(), 'market': hyperliquid_bot.snapshot()})
        if parsed.path == '/api/hermes/run':
            task_id = parse_qs(parsed.query).get('task_id', [''])[0]
            if not task_id or len(task_id) > 80:
                return self.send_json({'error': 'task_id fehlt'}, 400)
            if not _task_owned(self, task_id):
                return self.send_json({'error': 'Task-Zugriff verweigert'}, 403)
            current, error = _native_task_cli(['show', task_id])
            if not current:
                return self.send_json({'error': 'Native Hermes task lookup failed', 'reason': error}, 503)
            return self.send_json(_task_payload(task_id, current))

        if parsed.path == '/api/fantasy':
            q = parse_qs(parsed.query)
            return self.send_json(fantasy_data({'username': q.get('username', ['schn4psy'])[0], 'season': q.get('season', ['2026'])[0], 'week': q.get('week', ['4'])[0], 'include_free_agents': q.get('include_free_agents', ['true'])[0], 'free_agent_limit': min(int(q.get('free_agent_limit', ['25'])[0]), 200)}))
        if parsed.path == '/fantasy': self.path='/fantasy.html'
        if parsed.path == '/trading': self.path='/trading.html'
        if parsed.path == '/api/odds': return self.send_json(odds_feed())
        if parsed.path == '/betting': self.path='/betting.html'
        if parsed.path == '/': self.path='/index.html'
        file = ROOT / self.path.lstrip('/')
        if file.is_file() and ROOT in file.parents:
            body=file.read_bytes(); types={'.html':'text/html; charset=utf-8','.css':'text/css; charset=utf-8','.js':'text/javascript; charset=utf-8','.json':'application/json; charset=utf-8'}; self.send_response(200); self.send_header('Content-Type',types.get(file.suffix,'application/octet-stream'))
            if parsed.path in ('/betting', '/betting.html') and not _dashboard_csrf(self):
                self.send_header('Set-Cookie', f'dashboard_csrf={secrets.token_urlsafe(24)}; SameSite=Strict; Path=/')
            self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body); return
        self.send_error(404)
    def do_POST(self):
        parsed = urlsplit(self.path)
        if parsed.path.startswith('/api/finance/'):
            return self.finance_post(parsed.path)
        if parsed.path in ('/api/engineering-status', '/api/telemetry'):
            return self.send_json({'error': 'Method not allowed'}, 405, allow='GET')
        if parsed.path == '/api/conversation':
            try:
                payload = json.loads(self.read_body())
                message = payload.get('message') if isinstance(payload, dict) else None
                result = conversation(message)
            except (ValueError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
                return self.send_json({'error': str(exc)[:200]}, 400)
            return self.send_json(result, 200 if 'response' in result else 503)
        if parsed.path == '/api/betting/paper-bets':
            cookie = _dashboard_csrf(self)
            if not cookie or not hmac.compare_digest(cookie, self.headers.get('X-CSRF-Token', '')):
                return self.send_json({'error': 'CSRF-Prüfung fehlgeschlagen'}, 403)
            try:
                result = save_paper_bet(json.loads(self.read_body()))
            except (ValueError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
                return self.send_json({'error': str(exc)[:200]}, 400)
            return self.send_json(result, 201)
        if parsed.path.startswith('/api/betting/paper-bets/') and parsed.path.endswith('/settle'):
            cookie = _dashboard_csrf(self)
            if not cookie or not hmac.compare_digest(cookie, self.headers.get('X-CSRF-Token', '')):
                return self.send_json({'error': 'CSRF-Prüfung fehlgeschlagen'}, 403)
            bet_id = parsed.path.split('/')[-2]
            try:
                payload = json.loads(self.read_body())
                result = settle_paper_bet(bet_id, payload.get('outcome') if isinstance(payload, dict) else None)
            except (ValueError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
                return self.send_json({'error': str(exc)[:200]}, 400)
            return self.send_json(result, 200)
        if parsed.path == '/api/hermes/run':
            cookie = _dashboard_csrf(self)
            if not cookie or not hmac.compare_digest(cookie, self.headers.get('X-CSRF-Token', '')):
                return self.send_json({'error': 'CSRF-Prüfung fehlgeschlagen'}, 403)
            try:
                payload = json.loads(self.read_body()); run = hermes_run(payload.get('message') if isinstance(payload, dict) else None)
            except (ValueError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
                return self.send_json({'error': str(exc)[:200]}, 400)
            if run.get('task_id'):
                _remember_hermes_task(run['task_id'], cookie)
            return self.send_json(run, 200 if run.get('status') in ('todo', 'ready', 'running', 'done', 'completed', 'failed', 'blocked') else 503)
        self.send_error(404)
    def read_body(self):
        length = int(self.headers.get('Content-Length', '0'))
        if length > FINANCE_MAX_UPLOAD: raise ValueError('Anfrage zu groß')
        return self.rfile.read(length)
    def finance_get(self, path):
        if path == '/api/finance/login': return self.send_json({'authenticated': bool(finance_session(self))})
        session = finance_require(self)
        if not session: return
        con = finance_connect()
        try:
            if path == '/api/finance/state':
                accounts = [dict(x) for x in con.execute('SELECT * FROM accounts ORDER BY name')]
                tx = [dict(x) for x in con.execute('SELECT t.*, a.name account_name FROM transactions t JOIN accounts a ON a.id=t.account_id ORDER BY booked_on DESC, id DESC LIMIT 200')]
                rules = [dict(x) for x in con.execute('SELECT * FROM rules ORDER BY name')]
                imports = [dict(x) for x in con.execute('SELECT * FROM imports ORDER BY id DESC LIMIT 20')]
                unclear = [x for x in tx if x['review_status'] == 'unclear']
                return self.send_json({'csrf': session['csrf'], 'accounts': accounts, 'transactions': tx, 'unclear': unclear, 'rules': rules, 'imports': imports, 'password_default': os.environ.get('FINANCE_PASSWORD') is None})
            if path == '/api/finance/audit': return self.send_json({'items': [dict(x) for x in con.execute('SELECT * FROM audit_log ORDER BY id DESC LIMIT 100')]})
            self.send_json({'error': 'Nicht gefunden'}, 404)
        finally: con.close()
    def finance_post(self, path):
        if path == '/api/finance/login':
            try: payload = json.loads(self.read_body() or b'{}')
            except (ValueError, json.JSONDecodeError): return self.send_json({'error': 'Ungültiges JSON'}, 400)
            if not finance_password_ok(payload.get('password', '')): return self.send_json({'error': 'Passwort falsch'}, 401)
            sid = secrets.token_urlsafe(32); FINANCE_SESSIONS[sid] = {'csrf': secrets.token_urlsafe(24), 'created': datetime.now().isoformat()}
            self.send_response(200); self.send_header('Set-Cookie', f'finance_session={sid}; HttpOnly; SameSite=Strict; Path=/'); self.send_header('Content-Type', 'application/json'); self.end_headers(); self.wfile.write(b'{"ok":true}'); return
        session = finance_require(self, csrf=True)
        if not session: return
        con = finance_connect()
        try:
            try: payload = json.loads(self.read_body() or b'{}')
            except (ValueError, json.JSONDecodeError): return self.send_json({'error': 'Ungültiges JSON'}, 400)
            if path == '/api/finance/import/preview':
                account_id = int(payload['account_id']); raw = __import__('base64').b64decode(payload['content'], validate=True)
                account = con.execute('SELECT kind FROM accounts WHERE id=?', (account_id,)).fetchone()
                if not account or len(raw) > FINANCE_MAX_UPLOAD: raise ValueError('Konto oder Datei ungültig')
                rows = finance_parse_csv(raw, account['kind']); token = secrets.token_urlsafe(18); FINANCE_PREVIEWS[token] = {'account_id': account_id, 'filename': str(payload.get('filename', 'import.csv'))[:120], 'rows': rows}
                return self.send_json({'preview_id': token, 'rows': rows[:100], 'count': len(rows), 'encoding_hint': 'automatisch erkannt'})
            if path == '/api/finance/import/commit':
                preview = FINANCE_PREVIEWS.pop(str(payload.get('preview_id', '')), None)
                if not preview: return self.send_json({'error': 'Vorschau abgelaufen'}, 400)
                now = datetime.now().isoformat(timespec='seconds'); cur = con.execute('INSERT INTO imports(account_id, filename, created_at, status, row_count) VALUES (?, ?, ?, ?, ?)', (preview['account_id'], preview['filename'], now, 'committed', len(preview['rows'])))
                import_id = cur.lastrowid; added = 0
                for row in preview['rows']:
                    try:
                        con.execute('INSERT INTO transactions(account_id, import_id, booked_on, description, amount_cents, kind, review_status, fingerprint, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)', (preview['account_id'], import_id, row['booked_on'], row['description'], row['amount_cents'], row['kind'], row['review_status'], row['fingerprint'], now)); added += 1
                    except sqlite3.IntegrityError: pass
                finance_audit(con, 'import', f'import_id={import_id}; rows={added}; duplicates={len(preview["rows"]) - added}'); con.commit(); return self.send_json({'ok': True, 'added': added, 'duplicates': len(preview['rows']) - added})
            if path == '/api/finance/transactions/confirm':
                ids = [int(x) for x in payload.get('ids', [])][:500]; status = payload.get('status', 'clear')
                if status not in ('clear', 'suggested', 'unclear'): raise ValueError('Status ungültig')
                if ids: con.execute(f"UPDATE transactions SET review_status=? WHERE id IN ({','.join('?' for _ in ids)})", [status] + ids)
                finance_audit(con, 'confirm', f'status={status}; count={len(ids)}'); con.commit(); return self.send_json({'ok': True})
            if path == '/api/finance/import/rollback':
                import_id = int(payload['import_id']); count = con.execute('SELECT COUNT(*) FROM transactions WHERE import_id=?', (import_id,)).fetchone()[0]; con.execute('DELETE FROM transactions WHERE import_id=?', (import_id,)); con.execute("UPDATE imports SET status='rolled_back' WHERE id=?", (import_id,)); finance_audit(con, 'rollback', f'import_id={import_id}; rows={count}'); con.commit(); return self.send_json({'ok': True, 'deleted': count})
            if path == '/api/finance/rules/save':
                rule_id = payload.get('id'); name, pattern, category = [str(payload.get(k, '')).strip()[:120] for k in ('name', 'pattern', 'category')]
                if not name or not pattern or not category: raise ValueError('Regel braucht Name, Muster und Kategorie')
                if rule_id: con.execute('UPDATE rules SET name=?, pattern=?, category=?, active=? WHERE id=?', (name, pattern, category, int(bool(payload.get('active', True))), int(rule_id)))
                else: con.execute('INSERT INTO rules(name, pattern, category) VALUES (?, ?, ?)', (name, pattern, category))
                finance_audit(con, 'rule', name); con.commit(); return self.send_json({'ok': True})
            if path == '/api/finance/rules/toggle':
                rule_id = int(payload['id']); con.execute('UPDATE rules SET active = CASE active WHEN 1 THEN 0 ELSE 1 END WHERE id=?', (rule_id,)); finance_audit(con, 'rule_toggle', str(rule_id)); con.commit(); return self.send_json({'ok': True})
            self.send_json({'error': 'Nicht gefunden'}, 404)
        except (KeyError, TypeError, ValueError, UnicodeError, sqlite3.Error) as exc:
            self.send_json({'error': str(exc)[:200]}, 400)
        finally: con.close()
    def send_json(self, obj, status=200, allow=None):
        body=json.dumps(obj, ensure_ascii=False).encode(); self.send_response(status); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Cache-Control','no-store'); self.send_header('Content-Length',str(len(body)))
        if allow: self.send_header('Allow', allow)
        self.end_headers(); self.wfile.write(body)
    def log_message(self,*args): pass

if __name__ == '__main__':
    finance_connect().close(); finance_backup()
    ThreadingHTTPServer(('0.0.0.0',8787),Handler).serve_forever()
