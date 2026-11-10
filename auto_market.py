#!/usr/bin/env python3
import os
import re
import math
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


ROOT = Path(__file__).parent
DEFAULT_MAX_PRICE_EUR = 40000
DEFAULT_MAX_DISTANCE_KM = 250
SEARCH_PROFILES = (
    {
        'id': 'vw_multivan_long',
        'label': 'VW Bus Multivan lang',
        'min_year': 2025,
        'max_mileage_km': 40000,
        'long_version_required': True,
    },
    {
        'id': 'ford_tourneo_custom_long',
        'label': 'Ford Tourneo Custom lang',
        'min_year': 2025,
        'max_mileage_km': 40000,
        'long_version_required': True,
    },
)
PROFILE_IDS = {profile['id'] for profile in SEARCH_PROFILES}


def _db_path():
    return Path(os.environ.get('AUTO_MARKET_DB', str(ROOT / 'auto-market.sqlite3'))).expanduser()


def _now():
    return datetime.now().astimezone().isoformat(timespec='seconds')


def _number(value, integer=False):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return int(value) if integer else float(value)
    raw = re.sub(r'[^0-9,.-]', '', str(value).strip())
    if not raw:
        return None
    if integer:
        if raw.isdigit():
            return int(raw)
        if re.fullmatch(r'\d{1,3}(?:\.\d{3})+', raw):
            return int(raw.replace('.', ''))
        return None
    if re.fullmatch(r'\d+', raw):
        return float(raw)
    if re.fullmatch(r'\d+[.,]\d+', raw):
        return float(raw.replace(',', '.'))
    if re.fullmatch(r'\d{1,3}(?:\.\d{3})+,\d+', raw):
        return float(raw.replace('.', '').replace(',', '.'))
    return None


def _text(value):
    return str(value or '').strip()


def _url(value):
    raw = _text(value)
    parsed = urlsplit(raw)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        return None
    hostname = parsed.hostname.lower() if parsed.hostname else ''
    if not hostname:
        return None
    netloc = hostname
    if parsed.port:
        netloc += ':' + str(parsed.port)
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path.rstrip('/') or '/', '', ''))


def normalize_candidate(candidate):
    if not isinstance(candidate, dict):
        return None
    profile = _text(candidate.get('profile'))
    profile_config = next((item for item in SEARCH_PROFILES if item['id'] == profile), None)
    if not profile_config:
        return None
    title = _text(candidate.get('title'))
    source = _text(candidate.get('source'))
    location = _text(candidate.get('location'))
    long_evidence = _text(candidate.get('long_version_evidence'))
    accident_evidence = _text(candidate.get('accident_free_evidence'))
    image_url = _url(candidate.get('image_url'))
    canonical_url = _url(candidate.get('url'))
    price = _number(candidate.get('price_eur'), integer=True)
    mileage = _number(candidate.get('mileage_km'), integer=True)
    year = _number(candidate.get('year'), integer=True)
    distance = _number(candidate.get('distance_km'))
    if not all((title, source, location, long_evidence, accident_evidence, image_url, canonical_url)):
        return None
    if price is None or mileage is None or year is None or distance is None:
        return None
    if min(price, mileage, year, distance) < 0:
        return None
    title_lower = title.lower()
    if profile == 'vw_multivan_long' and 'multivan' not in title_lower:
        return None
    if profile == 'ford_tourneo_custom_long' and not all(term in title_lower for term in ('tourneo', 'custom')):
        return None
    if not re.search(r'\b(?:lang|long|l2|lwb)\b', title_lower) or not re.search(r'\b(?:lang|long|l2|lwb)\b', long_evidence.lower()):
        return None
    if year < profile_config['min_year'] or mileage > profile_config['max_mileage_km']:
        return None
    return {
        'profile': profile,
        'title': title[:300],
        'price_eur': price,
        'mileage_km': mileage,
        'year': year,
        'location': location[:200],
        'distance_km': round(distance, 1),
        'image_url': image_url,
        'url': canonical_url,
        'source': source[:120],
        'long_version_evidence': long_evidence[:500],
        'accident_free_evidence': accident_evidence[:500],
    }


def matches_filters(listing, max_price_eur, max_distance_km):
    try:
        max_price = int(max_price_eur)
        max_distance = float(max_distance_km)
    except (TypeError, ValueError):
        return False
    return bool(listing and listing.get('price_eur', max_price + 1) <= max_price and listing.get('distance_km', max_distance + 1) <= max_distance)


def auto_connect():
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys = ON')
    con.executescript('''
        CREATE TABLE IF NOT EXISTS listings (
            id INTEGER PRIMARY KEY,
            profile TEXT NOT NULL,
            title TEXT NOT NULL,
            price_eur INTEGER NOT NULL,
            mileage_km INTEGER NOT NULL,
            year INTEGER NOT NULL,
            location TEXT NOT NULL,
            distance_km REAL NOT NULL,
            image_url TEXT NOT NULL,
            url TEXT UNIQUE NOT NULL,
            source TEXT NOT NULL,
            long_version_evidence TEXT NOT NULL,
            accident_free_evidence TEXT NOT NULL,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            last_checked TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            last_error TEXT
        );
        CREATE TABLE IF NOT EXISTS favorites (
            listing_id INTEGER PRIMARY KEY,
            favorite INTEGER NOT NULL DEFAULT 1,
            FOREIGN KEY(listing_id) REFERENCES listings(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS refresh_runs (
            id INTEGER PRIMARY KEY,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL,
            inserted INTEGER NOT NULL DEFAULT 0,
            updated INTEGER NOT NULL DEFAULT 0,
            removed INTEGER NOT NULL DEFAULT 0,
            failed INTEGER NOT NULL DEFAULT 0,
            error TEXT
        );
        CREATE INDEX IF NOT EXISTS listings_active_idx ON listings(active, profile, price_eur, distance_km);
    ''')
    return con


def list_active_listings(max_price_eur=DEFAULT_MAX_PRICE_EUR, max_distance_km=DEFAULT_MAX_DISTANCE_KM):
    with closing(auto_connect()) as con:
        rows = con.execute('''
            SELECT l.*, COALESCE(f.favorite, 0) AS favorite
            FROM listings l
            LEFT JOIN favorites f ON f.listing_id = l.id
            WHERE l.active = 1 AND l.price_eur <= ? AND l.distance_km <= ?
            ORDER BY l.profile, l.price_eur, l.distance_km, l.id DESC
        ''', (int(max_price_eur), float(max_distance_km))).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item['favorite'] = bool(item['favorite'])
        result.append(item)
    return result


def set_favorite(listing_id, favorite):
    with closing(auto_connect()) as con:
        row = con.execute('SELECT id FROM listings WHERE id = ? AND active = 1', (int(listing_id),)).fetchone()
        if not row:
            raise ValueError('Listing nicht gefunden')
        if bool(favorite):
            con.execute('INSERT OR REPLACE INTO favorites(listing_id, favorite) VALUES (?, 1)', (int(listing_id),))
        else:
            con.execute('DELETE FROM favorites WHERE listing_id = ?', (int(listing_id),))
        con.commit()
    return {'listing_id': int(listing_id), 'favorite': bool(favorite)}


def _upsert(con, listing, timestamp):
    existing = con.execute('SELECT id FROM listings WHERE url = ?', (listing['url'],)).fetchone()
    fields = ('profile', 'title', 'price_eur', 'mileage_km', 'year', 'location', 'distance_km', 'image_url', 'url', 'source', 'long_version_evidence', 'accident_free_evidence')
    values = [listing[field] for field in fields]
    if existing:
        con.execute('''UPDATE listings SET profile=?, title=?, price_eur=?, mileage_km=?, year=?, location=?, distance_km=?, image_url=?, source=?, long_version_evidence=?, accident_free_evidence=?, last_seen=?, last_checked=?, active=1, last_error=NULL WHERE url=?''', (*values[:8], *values[9:], timestamp, timestamp, listing['url']))
        return 'updated'
    con.execute('''INSERT INTO listings(profile, title, price_eur, mileage_km, year, location, distance_km, image_url, url, source, long_version_evidence, accident_free_evidence, first_seen, last_seen, last_checked) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''', (*values, timestamp, timestamp, timestamp))
    return 'inserted'


def refresh_listings(discover, fetch):
    started = _now()
    inserted = updated = removed = failed = 0
    discovery_error = None
    try:
        discovered = discover() or []
    except Exception as exc:
        discovered = []
        discovery_error = type(exc).__name__
        failed += 1
    with closing(auto_connect()) as con:
        run = con.execute('INSERT INTO refresh_runs(started_at, status) VALUES (?, ?)', (started, 'running'))
        seen = set()
        timestamp = _now()
        for candidate in discovered:
            listing = normalize_candidate(candidate)
            if not listing:
                failed += 1
                continue
            seen.add(listing['url'])
            if _upsert(con, listing, timestamp) == 'inserted':
                inserted += 1
            else:
                updated += 1
        known = con.execute('SELECT id, url FROM listings WHERE active = 1').fetchall()
        for row in known:
            if row['url'] in seen:
                continue
            try:
                checked = fetch(row['url'])
            except Exception as exc:
                checked = None
                failed += 1
                con.execute('UPDATE listings SET last_checked=?, last_error=? WHERE id=?', (_now(), type(exc).__name__, row['id']))
            if isinstance(checked, dict) and int(checked.get('status_code', 0) or 0) in (404, 410):
                con.execute('DELETE FROM listings WHERE id = ?', (row['id'],))
                removed += 1
            elif checked is not None:
                con.execute('UPDATE listings SET last_checked=?, last_error=NULL WHERE id=?', (_now(), row['id']))
        status = 'error' if discovery_error else 'ok'
        error = discovery_error
        con.execute('UPDATE refresh_runs SET finished_at=?, status=?, inserted=?, updated=?, removed=?, failed=?, error=? WHERE id=?', (_now(), status, inserted, updated, removed, failed, error, run.lastrowid))
        con.commit()
    return {'status': status, 'inserted': inserted, 'updated': updated, 'removed': removed, 'failed': failed, 'error': discovery_error}
