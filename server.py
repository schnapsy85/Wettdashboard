#!/usr/bin/env python3
import json, os, subprocess, urllib.request
from urllib.parse import urlsplit
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

def router_stats():
    # Nine Router endpoint varies by deployment; never invent usage values.
    for path in ('/usage', '/v1/usage', '/api/usage', '/health'):
        try:
            req = urllib.request.Request('http://127.0.0.1:20128' + path, headers={'Accept':'application/json'})
            with urllib.request.urlopen(req, timeout=2) as r:
                data = json.loads(r.read())
            if isinstance(data, dict) and any(k in data for k in ('tokens','usage','prompt_tokens','total_tokens')):
                return {'status':'available','source':path,'data':data}
        except Exception:
            pass
    return {'status':'unavailable','source':'Nine Router','reason':'Usage endpoint did not return measurable token data'}

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
        if self.path == '/betting': self.path='/betting.html'
        if self.path == '/': self.path='/index.html'
        file = ROOT / self.path.lstrip('/')
        if file.is_file() and ROOT in file.parents:
            body=file.read_bytes(); self.send_response(200); self.send_header('Content-Type','text/html; charset=utf-8'); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body); return
        self.send_error(404)
    def send_json(self, obj):
        body=json.dumps(obj).encode(); self.send_response(200); self.send_header('Content-Type','application/json'); self.send_header('Cache-Control','no-store'); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)
    def log_message(self,*args): pass

ThreadingHTTPServer(('0.0.0.0',8787),Handler).serve_forever()
