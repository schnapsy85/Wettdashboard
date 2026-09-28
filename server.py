#!/usr/bin/env python3
import json, os, socket, subprocess, urllib.request
from collections import defaultdict
from urllib.parse import urlencode
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).parent

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
    model['status'] = 'CALL' if model['edge'] >= 0.05 and model['confidence'] >= 0.68 else 'NO_CALL'
    model['tip_text'] = f"{model['status']} · {model['selection']} · {model['market']} · Fair {model['fair_quote']:.2f} · Markt {model['market_price']:.2f}"
    return model

def router_stats():
    # Health is separate from usage; never confuse missing metrics with offline router.
    try:
        with socket.create_connection(('127.0.0.1', 20128), timeout=2):
            health = 'online'
    except OSError as exc:
        return {'status':'offline','health':'offline','source':'Nine Router','reason':type(exc).__name__}
    return {'status':'online','health':health,'source':'Nine Router','endpoint':'http://127.0.0.1:20128/v1','usage':'UNAVAILABLE','reason':'Nine Router stellt keinen messbaren Usage-Endpunkt bereit'}

def systems():
    def run(cmd):
        try: return subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL, timeout=2).strip()
        except Exception: return 'UNAVAILABLE'
    return {'gateway':run(['systemctl','--user','is-active','hermes-gateway.service']),
            'router':run(['systemctl','--user','is-active','nine-router.service']),
            'dashboard':run(['systemctl','--user','is-active','dashboard.service']),
            'disk':run(['sh','-c','df -h / | awk \'NR==2 {print $5}\'']),
            'load':run(['sh','-c','awk "{print $1, $2, $3}" /proc/loadavg'])}

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/api/systems': return self.send_json(systems())
        if self.path == '/api/tokens': return self.send_json(router_stats())
        if self.path == '/api/odds/status': return self.send_json(odds_status())
        if self.path == '/api/odds': return self.send_json(odds_feed())
        if self.path == '/betting': self.path='/betting.html'
        if self.path == '/': self.path='/index.html'
        file = ROOT / self.path.lstrip('/')
        if file.is_file() and ROOT in file.parents:
            body=file.read_bytes(); types={'.html':'text/html; charset=utf-8','.css':'text/css; charset=utf-8','.js':'text/javascript; charset=utf-8','.json':'application/json; charset=utf-8'}; self.send_response(200); self.send_header('Content-Type',types.get(file.suffix,'application/octet-stream')); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body); return
        self.send_error(404)
    def send_json(self, obj):
        body=json.dumps(obj).encode(); self.send_response(200); self.send_header('Content-Type','application/json'); self.send_header('Cache-Control','no-store'); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)
    def log_message(self,*args): pass

if __name__ == '__main__':
    ThreadingHTTPServer(('0.0.0.0',8787),Handler).serve_forever()
