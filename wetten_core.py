"""Private bet journal for the Hermes dashboard.

No sports-data API is used. Automated settlement is fail-closed: HTML score
checks require an explicitly approved host and an unambiguous FINAL score.
"""
from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.robotparser import RobotFileParser
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("WETTEN_DB_PATH", str(ROOT / "wetten.sqlite3")))
SESSIONS = {}
LOCK = threading.Lock()
LAST_POLL = {"at": None, "result": "not_started", "scanned": 0, "updated": 0}
ROBOTS_CACHE = {}
FAILED_LOGINS = {}
INTERVAL = 300
BOOKMAKERS = {"bet365", "Winamax", "neo.bet"}
MARKETS = {"moneyline", "1x2", "spread", "total", "btts", "double_chance", "dnb", "special"}
STATES = {"open", "won", "lost", "push", "needs_review"}
TERMINAL = {"won", "lost", "push"}

def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def connection():
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=10000")
    return con

def backup_if_due():
    """SQLite online backup, at most once per UTC calendar day."""
    if not DB_PATH.exists():
        return
    target_dir = DB_PATH.parent / "backups" / "wetten"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / (datetime.now(timezone.utc).strftime("%Y-%m-%d") + ".sqlite3")
    if target.exists():
        return
    temporary = target.with_suffix(".sqlite3.tmp")
    with connection() as source, sqlite3.connect(temporary) as destination:
        source.backup(destination)
    os.chmod(temporary, 0o600)
    temporary.replace(target)


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connection() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS tickets(
          id INTEGER PRIMARY KEY, bookmaker TEXT NOT NULL, placed_at TEXT NOT NULL,
          stake_cents INTEGER NOT NULL CHECK(stake_cents>0), odds TEXT NOT NULL,
          potential_cents INTEGER NOT NULL, payout_cents INTEGER,
          type TEXT NOT NULL CHECK(type IN ('single','combo')),
          status TEXT NOT NULL DEFAULT 'open', manual_override INTEGER NOT NULL DEFAULT 0,
          note TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT 'manual',
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL, deleted_at TEXT
        );
        CREATE TABLE IF NOT EXISTS legs(
          id INTEGER PRIMARY KEY, ticket_id INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
          sport TEXT NOT NULL, league TEXT NOT NULL DEFAULT '', home TEXT NOT NULL,
          away TEXT NOT NULL, kickoff TEXT NOT NULL DEFAULT '', market TEXT NOT NULL,
          selection TEXT NOT NULL, line TEXT, period TEXT NOT NULL DEFAULT 'full_game',
          odds TEXT NOT NULL, result_url TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL DEFAULT 'open', home_score INTEGER, away_score INTEGER,
          checked_at TEXT, evidence TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS audit(
          id INTEGER PRIMARY KEY, ticket_id INTEGER, action TEXT NOT NULL,
          old_status TEXT, new_status TEXT, reason TEXT NOT NULL,
          occurred_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_ticket_time ON tickets(placed_at);
        CREATE INDEX IF NOT EXISTS idx_leg_status ON legs(status);
        """)
    try:
        os.chmod(DB_PATH, 0o600)
    except OSError:
        pass

def decimal_odds(value):
    try:
        v = Decimal(str(value).replace(",", "."))
    except (InvalidOperation, ValueError):
        raise ValueError("Ungültige Quote")
    if not v.is_finite() or not (Decimal("1") <= v <= Decimal("10000")):
        raise ValueError("Quote außerhalb des Bereichs")
    return v

def euros_to_cents(value):
    try:
        v = Decimal(str(value).replace(",", "."))
    except (InvalidOperation, ValueError):
        raise ValueError("Ungültiger Geldbetrag")
    if not v.is_finite() or not (Decimal("0") < v <= Decimal("1000000")):
        raise ValueError("Ungültiger Einsatz")
    if v.as_tuple().exponent < -2:
        raise ValueError("Maximal zwei Nachkommastellen")
    return int(v * 100)

def return_cents(stake_cents, odds):
    calculated = int((Decimal(stake_cents) * odds).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    if calculated > 999_999_999_999:
        raise ValueError("Auszahlung überschreitet das Datenbanklimit")
    return calculated

def clean(value, maximum=120):
    v = str(value or "").strip()
    if len(v) > maximum or any(ord(c) < 32 for c in v):
        raise ValueError("Ungültiges Textfeld")
    return v

def create_ticket(payload):
    bookmaker = clean(payload.get("bookmaker"), 30)
    if bookmaker not in BOOKMAKERS:
        raise ValueError("Unbekannter Buchmacher")
    placed_at = clean(payload.get("placed_at"), 35)
    try:
        datetime.fromisoformat(placed_at.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("Ungültiges Datum")
    stake = euros_to_cents(payload.get("stake"))
    supplied = payload.get("legs")
    if not isinstance(supplied, list) or not 1 <= len(supplied) <= 15:
        raise ValueError("1 bis 15 Auswahlen erforderlich")
    legs = []
    odds = Decimal(1)
    for item in supplied:
        if not isinstance(item, dict):
            raise ValueError("Ungültige Auswahl")
        market = clean(item.get("market"), 30)
        if market not in MARKETS:
            raise ValueError("Unbekannter Wettmarkt")
        period = clean(item.get("period", "full_game"), 25)
        if period not in ("full_game", "regulation"):
            raise ValueError("Ungültige Spielperiode")
        leg_odds = decimal_odds(item.get("odds"))
        line = item.get("line")
        if market in ("spread", "total"):
            try:
                line = Decimal(str(line).replace(",", "."))
                if not line.is_finite() or abs(line) > 1000:
                    raise ValueError()
            except (InvalidOperation, ValueError, TypeError):
                raise ValueError("Handicap-/Torlinie fehlt")
            line = str(line)
        else:
            line = None
        url = clean(item.get("result_url"), 500)
        if url:
            parsed = urlparse(url)
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("Ergebnislink muss eine HTTPS-Webseite sein")
        home, away = clean(item.get("home")), clean(item.get("away"))
        if not home or not away or home.casefold() == away.casefold():
            raise ValueError("Heim- und Gastteam erforderlich")
        sport = clean(item.get("sport"), 40)
        selection = clean(item.get("selection"), 50).lower()
        if not sport or not selection:
            raise ValueError("Sport und Tipp erforderlich")
        leg = (sport, clean(item.get("league")), home, away, clean(item.get("kickoff"), 35),
               market, selection, line, period, str(leg_odds), url)
        legs.append(leg)
        odds *= leg_odds
    with connection() as con:
        cur = con.execute("""INSERT INTO tickets(bookmaker,placed_at,stake_cents,odds,potential_cents,type,note,
                       created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                       (bookmaker, placed_at, stake, str(odds), return_cents(stake, odds),
                        "single" if len(legs) == 1 else "combo",
                        clean(payload.get("note"), 500), now(), now()))
        ticket_id = cur.lastrowid
        for leg in legs:
            con.execute("""INSERT INTO legs(ticket_id,sport,league,home,away,kickoff,market,selection,line,period,odds,result_url)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", (ticket_id, *leg))
        audit(con, ticket_id, "create", None, "open", "Manuell angelegte Wette")
    return ticket_id

def audit(con, ticket_id, action, old, new, reason):
    con.execute("INSERT INTO audit(ticket_id,action,old_status,new_status,reason,occurred_at) VALUES(?,?,?,?,?,?)",
                (ticket_id, action, old, new, reason, now()))

def view_data():
    with connection() as con:
        rows = [dict(r) for r in con.execute("SELECT * FROM tickets WHERE deleted_at IS NULL ORDER BY placed_at DESC,id DESC LIMIT 1000")]
        if rows:
            ids = [r["id"] for r in rows]
            grouped = {}
            for leg in con.execute("SELECT * FROM legs WHERE ticket_id IN (" + ",".join("?" for _ in ids) + ") ORDER BY id", ids):
                grouped.setdefault(leg["ticket_id"], []).append(dict(leg))
            for r in rows:
                r["legs"] = grouped.get(r["id"], [])
        settled = [r for r in rows if r["status"] in TERMINAL]
        return {
          "tickets": rows,
          "summary": {
            "count": len(rows),
            "stake_cents": sum(r["stake_cents"] for r in rows),
            "open_stake_cents": sum(r["stake_cents"] for r in rows if r["status"] not in TERMINAL),
            "realized_profit_cents": sum((r["payout_cents"] or 0) - r["stake_cents"] for r in settled),
            "settled_stake_cents": sum(r["stake_cents"] for r in settled),
            "potential_cents": sum(r["potential_cents"] for r in rows if r["status"] not in TERMINAL),
          },
          "poller": dict(LAST_POLL)
        }

def manual_settle(payload):
    ticket_id = int(payload.get("id"))
    status = clean(payload.get("status"), 20)
    if status not in STATES:
        raise ValueError("Status ungültig")
    payout = 0
    if status == "won":
        payout = euros_to_cents(payload.get("payout"))
    elif status == "push":
        payout = None
    reason = clean(payload.get("reason"), 500)
    if len(reason) < 3:
        raise ValueError("Bitte eine Begründung angeben")
    with connection() as con:
        row = con.execute("SELECT * FROM tickets WHERE id=? AND deleted_at IS NULL", (ticket_id,)).fetchone()
        if row is None:
            raise ValueError("Wette nicht gefunden")
        if status == "push":
            payout = row["stake_cents"]
        con.execute("UPDATE tickets SET status=?,payout_cents=?,manual_override=1,updated_at=? WHERE id=?",
                    (status, payout if status in TERMINAL else None, now(), ticket_id))
        audit(con, ticket_id, "manual_settlement", row["status"], status, reason)

def delete_ticket(payload):
    ticket_id = int(payload.get("id"))
    with connection() as con:
        r = con.execute("SELECT status FROM tickets WHERE id=? AND deleted_at IS NULL", (ticket_id,)).fetchone()
        if r is None:
            raise ValueError("Wette nicht gefunden")
        con.execute("UPDATE tickets SET deleted_at=?,updated_at=? WHERE id=?", (now(), now(), ticket_id))
        audit(con, ticket_id, "delete", r["status"], r["status"], "Manuell gelöscht")

class VisibleHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.parts = []
    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "noscript"):
            self.hidden += 1
    def handle_endtag(self, tag):
        if tag in ("script", "style", "noscript"):
            self.hidden = max(0, self.hidden-1)
    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)

def extract_final_score(html, home, away):
    """Fail closed. Only unique adjacent visible scores + final status are accepted."""
    p = VisibleHTML()
    p.feed(html)
    visible = re.sub(r"\s+", " ", " ".join(p.parts))
    def norm(s):
        return re.escape(s).replace(r"\ ", r"\s+")
    # Exact home/away labels, order and both scores, with FINAL/Endstand nearby.
    pat = re.compile(norm(home) + r"\s+(\d{1,3})\s*[-:]\s*(\d{1,3})\s+" + norm(away), re.I)
    matches = list(pat.finditer(visible))
    if len(matches) != 1:
        return None
    match = matches[0]
    nearby = visible[max(0, match.start()-35):min(len(visible), match.end()+35)]
    if not re.search(r"\b(final|full\s*time|endstand|beendet|spielende)\b", nearby, re.I):
        return None
    return int(match.group(1)), int(match.group(2))

def evaluate(leg, home_score, away_score):
    if leg["period"] != "full_game":
        return "needs_review"  # HTML final score does not prove regulation result
    m, sel = leg["market"], leg["selection"]
    h, a = home_score, away_score
    if m in ("moneyline", "1x2", "dnb"):
        winner = "home" if h > a else "away" if a > h else "draw"
        if m == "dnb" and winner == "draw": return "push"
        return "won" if sel == winner else "lost"
    if m == "double_chance":
        winner = "1" if h > a else "2" if a > h else "x"
        return "won" if winner in {"1x": ("1","x"), "x2": ("x","2"), "12": ("1","2")}.get(sel, ()) else "lost"
    if m == "spread":
        spread = Decimal(leg["line"])
        actual = Decimal(h if sel == "home" else a) + spread
        opposite = Decimal(a if sel == "home" else h)
        return "won" if actual > opposite else "push" if actual == opposite else "lost"
    if m == "total":
        value = Decimal(h + a)
        threshold = Decimal(leg["line"])
        if value == threshold: return "push"
        return "won" if (value > threshold) == (sel == "over") else "lost"
    if m == "btts":
        yes = h > 0 and a > 0
        return "won" if yes == (sel == "yes") else "lost"
    return "needs_review"

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def read_robots(hostname):
    cached = ROBOTS_CACHE.get(hostname)
    if cached and time.time() - cached[0] < 86400:
        return cached[1]
    robot_url = "https://" + hostname + "/robots.txt"
    req = Request(robot_url, headers={"User-Agent": "HermesWetten/1.0"})
    with build_opener(NoRedirect()).open(req, timeout=8) as resp:
        if resp.status != 200 or "text/plain" not in resp.headers.get("Content-Type", ""):
            raise ValueError("robots.txt nicht überprüfbar")
        raw = resp.read(100_001)
        if len(raw) > 100_000: raise ValueError("robots.txt zu groß")
    robots = RobotFileParser()
    robots.parse(raw.decode("utf-8", errors="replace").splitlines())
    ROBOTS_CACHE[hostname] = (time.time(), robots)
    return robots


def html_result(url, home, away, allowed_hosts):
    p = urlparse(url)
    # Never follow an untrusted user URL; prevents SSRF via localhost/IPs.
    if p.scheme != "https" or p.hostname not in allowed_hosts or p.username or p.password or p.port not in (None, 443):
        return None, "Host nicht freigegeben"
    try:
        robots = read_robots(p.hostname)
        if not robots.can_fetch("HermesWetten/1.0", url):
            return None, "robots.txt verbietet Abruf"
        req = Request(url, headers={"User-Agent": "HermesWetten/1.0"})
        with build_opener(NoRedirect()).open(req, timeout=8) as resp:
            if resp.geturl() != url: return None, "Weiterleitung abgelehnt"
            if "text/html" not in resp.headers.get("Content-Type", ""):
                return None, "Kein HTML"
            html = resp.read(512_001)
        if len(html) > 512_000: return None, "HTML zu groß"
        result = extract_final_score(html.decode("utf-8", errors="replace"), home, away)
        return result, "Endstand aus HTML" if result else "Kein eindeutiges finales Ergebnis"
    except Exception as exc:
        return None, "Quellenseite nicht verfügbar (" + type(exc).__name__ + ")"

def recompute(con, ticket_id):
    ticket = con.execute("SELECT * FROM tickets WHERE id=? AND deleted_at IS NULL", (ticket_id,)).fetchone()
    if ticket is None or ticket["manual_override"]:
        return
    legs = list(con.execute("SELECT status,odds FROM legs WHERE ticket_id=?", (ticket_id,)))
    statuses = [row["status"] for row in legs]
    if "lost" in statuses: status = "lost"
    elif all(s in ("won","push") for s in statuses):
        status = "push" if all(s == "push" for s in statuses) else "won"
    elif "needs_review" in statuses: status = "needs_review"
    else: status = "open"
    payout = None
    if status == "lost": payout = 0
    if status == "push": payout = ticket["stake_cents"]
    if status == "won":
        odds = Decimal(1)
        for leg in legs:
            if leg["status"] == "won": odds *= Decimal(leg["odds"])
        payout = return_cents(ticket["stake_cents"], odds)
    if status != ticket["status"] or payout != ticket["payout_cents"]:
        con.execute("UPDATE tickets SET status=?,payout_cents=?,updated_at=? WHERE id=?",
                    (status, payout, now(), ticket_id))
        audit(con, ticket_id, "html_settlement", ticket["status"], status, "Bestätigtes öffentliches HTML-Endergebnis")

def check_once():
    hosts = {h.strip().lower() for h in os.environ.get("WETTEN_HTML_ALLOW_HOSTS","").split(",") if h.strip()}
    scanned = updated = 0
    with LOCK:
        try:
            if hosts:
                with connection() as con:
                    rows = [dict(row) for row in con.execute("""
                      SELECT l.* FROM legs l JOIN tickets t ON t.id=l.ticket_id
                      WHERE t.deleted_at IS NULL AND t.manual_override=0
                        AND l.status IN ('open','needs_review') AND l.result_url!=''
                      ORDER BY l.id LIMIT 100""")]
                    # De-duplicate requests for identical matches; never hammer a site.
                    cache = {}
                    for row in rows:
                        key = (row["result_url"], row["home"], row["away"])
                        if key not in cache:
                            cache[key] = html_result(*key, hosts)
                            time.sleep(0.2)
                        result, evidence = cache[key]
                        scanned += 1
                        if result:
                            state = evaluate(row, *result)
                            con.execute("""UPDATE legs SET status=?,home_score=?,away_score=?,checked_at=?,evidence=?
                                        WHERE id=?""", (state, *result, now(), row["result_url"] + " · " + evidence, row["id"]))
                            recompute(con, row["ticket_id"])
                            updated += 1
                        else:
                            con.execute("UPDATE legs SET checked_at=?,evidence=? WHERE id=?",
                                        (now(), evidence, row["id"]))
            LAST_POLL.update(at=now(), result="ok" if hosts else "disabled_no_approved_html_sources",
                             scanned=scanned, updated=updated)
        except Exception as exc:
            LAST_POLL.update(at=now(), result="error_" + type(exc).__name__, scanned=scanned, updated=updated)
    return dict(LAST_POLL)

def start_poller():
    init_db()
    def loop():
        while True:
            try:
                backup_if_due()
            except (OSError, sqlite3.Error) as exc:
                LAST_POLL.update(at=now(),result='backup_error_'+type(exc).__name__)
            check_once()
            time.sleep(INTERVAL)
    threading.Thread(target=loop, name="wetten-html-poller", daemon=True).start()

def configured():
    return len(os.environ.get("WETTEN_PASSWORD", "")) >= 12

def get_session(handler):
    cookie = handler.headers.get("Cookie", "")
    match = re.search(r"(?:^|;\s*)wetten_sid=([A-Za-z0-9_-]{32,128})", cookie)
    if not match: return None
    sid = match.group(1)
    session = SESSIONS.get(sid)
    if session and time.time() - session["created"] < 12*3600:
        return session
    SESSIONS.pop(sid, None)
    return None

def reply(handler, payload, code=200):
    handler.send_json(payload, code)

def handle_get(handler, path):
    if path == "/api/wetten/session":
        session = get_session(handler)
        return reply(handler, {"configured": configured(), "authenticated": bool(session),
                               "csrf": session["csrf"] if session else None})
    if not get_session(handler):
        return reply(handler, {"error": "Nicht angemeldet"}, 401)
    if path == "/api/wetten/data":
        return reply(handler, view_data())
    return reply(handler, {"error": "Nicht gefunden"}, 404)

def handle_post(handler, path):
    try:
        length = int(handler.headers.get("Content-Length", "0"))
        if not (0 <= length <= 18000): return reply(handler, {"error": "Anfrage zu groß"}, 413)
        data = json.loads(handler.rfile.read(length) or b"{}")
        if not isinstance(data, dict): raise ValueError("Ungültige Eingabe")
        if path == "/api/wetten/login":
            if not configured(): return reply(handler, {"error": "Serverpasswort nicht eingerichtet"}, 503)
            client_ip = handler.client_address[0]
            recent = [t for t in FAILED_LOGINS.get(client_ip, []) if time.time() - t < 900]
            FAILED_LOGINS[client_ip] = recent
            if len(recent) >= 5:
                return reply(handler, {"error": "Zu viele Versuche, später erneut anmelden"}, 429)
            if not hmac.compare_digest(str(data.get("password","")), os.environ["WETTEN_PASSWORD"]):
                FAILED_LOGINS[client_ip].append(time.time())
                return reply(handler, {"error": "Falsches Passwort"}, 401)
            FAILED_LOGINS.pop(client_ip, None)
            sid, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
            SESSIONS[sid] = {"csrf": csrf, "created": time.time()}
            handler.send_response(200)
            handler.send_header("Content-Type","application/json; charset=utf-8")
            handler.send_header("Cache-Control","no-store")
            secure = " Secure;" if os.environ.get("WETTEN_COOKIE_SECURE","1") != "0" else ""
            handler.send_header("Set-Cookie", "wetten_sid="+sid+"; HttpOnly; SameSite=Strict; Path=/api/wetten/;"+secure+" Max-Age=43200")
            body = json.dumps({"ok": True, "csrf": csrf}).encode()
            handler.send_header("Content-Length",str(len(body)))
            handler.end_headers()
            handler.wfile.write(body)
            return
        session = get_session(handler)
        if not session: return reply(handler, {"error": "Nicht angemeldet"}, 401)
        if not hmac.compare_digest(str(data.get("csrf","")), session["csrf"]):
            return reply(handler, {"error": "CSRF ungültig"}, 403)
        if path == "/api/wetten/create":
            ident = create_ticket(data)
            return reply(handler, {"ok": True, "id": ident}, 201)
        if path == "/api/wetten/settle":
            manual_settle(data)
            return reply(handler, {"ok": True})
        if path == "/api/wetten/delete":
            delete_ticket(data)
            return reply(handler, {"ok": True})
        if path == "/api/wetten/check":
            # no outbound scraping from web request; actual poller runs separately
            return reply(handler, {"ok": True, "poller": dict(LAST_POLL)})
        if path == "/api/wetten/logout":
            cookie = handler.headers.get("Cookie", "")
            match = re.search(r"(?:^|;\s*)wetten_sid=([A-Za-z0-9_-]{32,128})",cookie)
            if match: SESSIONS.pop(match.group(1),None)
            return reply(handler, {"ok": True})
        return reply(handler, {"error": "Nicht gefunden"},404)
    except (ValueError, KeyError, TypeError, sqlite3.Error, json.JSONDecodeError) as exc:
        return reply(handler, {"error": str(exc)[:200]}, 400)
