#!/usr/bin/env python3
import csv, hashlib, hmac, io, json, os, secrets, shutil, socket, sqlite3, subprocess, urllib.request
from collections import defaultdict
from datetime import date, datetime
from urllib.parse import parse_qs, urlencode, urlparse
import hyperliquid_bot

FANTASY_API = os.environ.get('FANTASY_API_URL', 'http://127.0.0.1:8091').rstrip('/')

def fantasy_data(query):
    try:
        url = FANTASY_API + '/intelligence/league/1389346968114851840?' + urlencode(query)
        req = urllib.request.Request(url, headers={'User-Agent': 'Hermes-Dashboard/1.0'})
        with urllib.request.urlopen(req, timeout=20) as response:
            return json.loads(response.read())
    except Exception as exc:
        return {'error': 'Fantasy Backend nicht erreichbar', 'reason': type(exc).__name__}

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).parent
FINANCE_DB = ROOT / 'finanzen.sqlite3'
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
    p = Path.home() / '.hermes' / '.env'
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
ODDS_CACHE = {'at': 0.0, 'data': None} 
ODDS_CACHE_TTL = 120.0

def odds_feed():
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

def normalize_event(event, league):
    markets = []
    for bookmaker in event.get('bookmakers', []):
        for market in bookmaker.get('markets', []):
            markets.append({'bookmaker':bookmaker.get('title','UNAVAILABLE'), 'market':market.get('key','UNAVAILABLE'), 'outcomes':market.get('outcomes',[])})
    result = {'id':event.get('id','UNAVAILABLE'), 'sport':event.get('sport_key','UNAVAILABLE'), 'competition':league,
            'home':event.get('home_team','UNAVAILABLE'), 'away':event.get('away_team','UNAVAILABLE'),
            'start':event.get('commence_time','UNAVAILABLE'), 'markets':markets}
    result.update(baseline_model(result))
    return result

def baseline_model(event):
    """Market-consensus model; no team features, fail closed on weak consensus."""
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
        probabilities = [row[1] for row in rows]
        probability = sum(probabilities) / len(probabilities)
        spread = (sum((p - probability) ** 2 for p in probabilities) / len(probabilities)) ** 0.5
        fair_quote = 1 / probability
        market_price = sorted(row[2] for row in rows)[len(rows) // 2]
        edge = market_price - fair_quote
        candidates.append((edge, {'market': market, 'selection': name,
            'model_probability': round(probability, 6), 'fair_quote': round(fair_quote, 4),
            'market_price': round(market_price, 4), 'edge': round(edge, 4),
            'edge_percent': round(edge / fair_quote * 100, 2),
            'confidence': round(max(0.0, min(1.0, 1 - spread * 4)), 3),
            'rationale': 'Markt-Konsensmodell; Overround normalisiert; mediane Marktquote.',
            'status': 'NO_CALL'}))
    if not candidates:
        return {'tip_text': 'NO_CALL · mindestens 3 unabhängige Buchmacher nötig', 'selection': None,
                'market': None, 'model_probability': None, 'fair_quote': None, 'market_price': None,
                'edge': None, 'edge_percent': None, 'confidence': 0, 'rationale': 'Keine verifizierte unabhängige Marktgruppe.',
                'status': 'NO_CALL'}
    _, model = max(candidates, key=lambda item: item[0])
    # Market consensus is not an independent prediction. Prefer hit probability and fail closed.
    model['status'] = 'CALL' if (model['edge'] >= 0.05 and model['confidence'] >= 0.68 and model['model_probability'] >= 0.35) else 'NO_CALL'
    if model['model_probability'] < 0.35:
        model['rationale'] = 'NO_CALL: Gewinnwahrscheinlichkeit unter 35%; Markt-Konsens allein reicht nicht für einen Tipp.'
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
    db = Path.home() / '.9router' / 'db' / 'data.sqlite'
    try:
        with sqlite3.connect(f'file:{db}?mode=ro', uri=True, timeout=1) as con:
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
    profiles_root = Path.home() / '.hermes' / 'profiles'
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
        parsed = urlparse(self.path)
        if parsed.path == '/finanzen.html': self.path = '/finanzen.html'
        if parsed.path.startswith('/api/finance/'):
            return self.finance_get(parsed.path)
        if self.path == '/api/systems': return self.send_json(systems())
        if self.path == '/api/agents': return self.send_json(agent_stats())
        if self.path == '/api/tokens': return self.send_json(router_stats())
        if self.path == '/api/odds/status': return self.send_json(odds_status())
        if self.path == '/api/trading/status': return self.send_json({'config': hyperliquid_bot.config(), 'strategy': hyperliquid_bot.strategy_contract(), 'market': hyperliquid_bot.snapshot()})
        if parsed.path == '/api/fantasy':
            q = parse_qs(parsed.query)
            return self.send_json(fantasy_data({'username': q.get('username', ['schn4psy'])[0], 'season': q.get('season', ['2026'])[0], 'week': q.get('week', ['4'])[0], 'include_free_agents': q.get('include_free_agents', ['true'])[0], 'free_agent_limit': min(int(q.get('free_agent_limit', ['25'])[0]), 200)}))
        if self.path == '/fantasy': self.path='/fantasy.html'
        if self.path == '/trading': self.path='/trading.html'
        if self.path == '/api/odds': return self.send_json(odds_feed())
        if self.path == '/betting': self.path='/betting.html'
        if self.path == '/': self.path='/index.html'
        file = ROOT / self.path.lstrip('/')
        if file.is_file() and ROOT in file.parents:
            body=file.read_bytes(); types={'.html':'text/html; charset=utf-8','.css':'text/css; charset=utf-8','.js':'text/javascript; charset=utf-8','.json':'application/json; charset=utf-8'}; self.send_response(200); self.send_header('Content-Type',types.get(file.suffix,'application/octet-stream')); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body); return
        self.send_error(404)
    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path.startswith('/api/finance/'):
            return self.finance_post(parsed.path)
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
    def send_json(self, obj, status=200):
        body=json.dumps(obj, ensure_ascii=False).encode(); self.send_response(status); self.send_header('Content-Type','application/json; charset=utf-8'); self.send_header('Cache-Control','no-store'); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)
    def log_message(self,*args): pass

if __name__ == '__main__':
    finance_connect().close(); finance_backup()
    ThreadingHTTPServer(('0.0.0.0',8787),Handler).serve_forever()
