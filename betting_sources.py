"""Small, fail-closed adapters for independent betting features."""

import csv
import hashlib
import io
import json
import os
import urllib.request
from datetime import datetime, timedelta, timezone


SOURCE_TIMEOUT_SECONDS = 10
MAX_RESPONSE_BYTES = 8_000_000
OBSERVATION_MAX_AGE = timedelta(days=90)
FOOTBALL_LEAGUES = {
    'Bundesliga': 'bl1',
    '2. Bundesliga': 'bl2',
    '3. Liga': 'bl3',
}
NFLVERSE_SCHEDULE_URL = 'https://github.com/nflverse/nflverse-data/releases/download/schedules/games.csv'


def _utc_now(value=None):
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _event_year(event):
    try:
        return datetime.fromisoformat(str(event['start']).replace('Z', '+00:00')).year
    except (KeyError, TypeError, ValueError):
        return _utc_now().year


def _fetch_payload(url, timeout=SOURCE_TIMEOUT_SECONDS, max_bytes=MAX_RESPONSE_BYTES):
    request = urllib.request.Request(url, headers={'User-Agent': 'HermesBetting/1.0'})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read(max_bytes + 1)
    if len(body) > max_bytes:
        raise ValueError('source response exceeds size limit')
    if url.lower().endswith('.csv'):
        return body.decode('utf-8-sig')
    return json.loads(body)


def _football_url(event):
    league = str(event.get('competition') or '')
    league_code = FOOTBALL_LEAGUES.get(league)
    if not league_code:
        return None
    env_key = f'OPENLIGADB_{league_code.upper()}_URL'
    return os.environ.get(env_key, f'https://api.openligadb.de/getmatchdata/{league_code}/{_event_year(event)}')


def _parse_timestamp(value):
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _parse_openligadb(payload, now):
    if not isinstance(payload, list):
        raise ValueError('OpenLigaDB payload malformed')
    observations = []
    stale = False
    for match in payload:
        if not isinstance(match, dict):
            continue
        if match.get('matchIsFinished') is False or match.get('MatchIsFinished') is False:
            continue
        results = match.get('MatchResults') or match.get('matchResults') or []
        result = max(results, key=lambda item: int(item.get('resultOrderID', item.get('resultOrderId', 0)) or 0)) if isinstance(results, list) and results else None
        observed_at = _parse_timestamp(match.get('matchDateTimeUTC') or match.get('MatchDateTimeUTC') or match.get('matchDateTime') or match.get('MatchDateTime'))
        if not isinstance(result, dict) or observed_at is None:
            continue
        if now - observed_at > OBSERVATION_MAX_AGE:
            stale = True
            continue
        try:
            home_value = result.get('PointsTeam1')
            away_value = result.get('PointsTeam2')
            if home_value is None:
                home_value = result.get('pointsTeam1')
            if away_value is None:
                away_value = result.get('pointsTeam2')
            home_score = int(home_value)
            away_score = int(away_value)
        except (KeyError, TypeError, ValueError):
            continue
        home_data = match.get('Team1') or match.get('team1') or {}
        away_data = match.get('Team2') or match.get('team2') or {}
        home = home_data.get('TeamName') or home_data.get('teamName')
        away = away_data.get('TeamName') or away_data.get('teamName')
        if not home or not away:
            continue
        observations.append({
            'kind': 'match_result',
            'event_id': str(match.get('MatchID') or match.get('matchID') or ''),
            'home': str(home),
            'away': str(away),
            'home_score': home_score,
            'away_score': away_score,
            'observed_at': observed_at.isoformat(),
        })
    if not observations:
        if stale:
            raise ValueError('OpenLigaDB observations stale')
        raise ValueError('OpenLigaDB payload has no completed matches')
    return observations


def _parse_nflverse(payload, now):
    if isinstance(payload, str):
        rows = csv.DictReader(io.StringIO(payload))
    elif isinstance(payload, list):
        rows = payload
    else:
        raise ValueError('nflverse payload malformed')
    observations = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        observed_at = _parse_timestamp(row.get('gameday') or row.get('game_date'))
        if observed_at is None or now - observed_at > OBSERVATION_MAX_AGE:
            continue
        try:
            home_score = int(row['home_score'])
            away_score = int(row['away_score'])
        except (KeyError, TypeError, ValueError):
            continue
        home = row.get('home_team')
        away = row.get('away_team')
        if not home or not away:
            continue
        observations.append({
            'kind': 'match_result',
            'event_id': str(row.get('game_id') or row.get('gsis_id') or ''),
            'home': str(home),
            'away': str(away),
            'home_score': home_score,
            'away_score': away_score,
            'observed_at': observed_at.isoformat(),
        })
    if not observations:
        raise ValueError('nflverse payload has no recent completed games')
    return observations


def _source_config(event, sport):
    if sport == 'football':
        url = _football_url(event)
        return {'name': 'OpenLigaDB', 'endpoint': url, 'parser': _parse_openligadb} if url else None
    if sport == 'nfl':
        return {
            'name': 'nflverse',
            'endpoint': os.environ.get('NFLVERSE_SCHEDULE_URL', NFLVERSE_SCHEDULE_URL),
            'parser': _parse_nflverse,
        }
    return None


def source_snapshot(event, sport, now=None, fetch_json=None):
    now = _utc_now(now)
    source = _source_config(event, sport)
    retrieved_at = now.isoformat()
    if source is None:
        return {
            'status': 'unavailable', 'observations': [], 'sources': [],
            'retrieved_at': retrieved_at, 'completeness': 0.0,
            'errors': [f'unsupported sport source: {sport}'],
        }
    fetcher = fetch_json or _fetch_payload
    source_row = {'name': source['name'], 'endpoint': source['endpoint'], 'status': 'unavailable'}
    try:
        payload = fetcher(source['endpoint'], SOURCE_TIMEOUT_SECONDS, MAX_RESPONSE_BYTES)
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
        source_row['payload_hash'] = hashlib.sha256(encoded).hexdigest()
        observations = source['parser'](payload, now)
        source_row['status'] = 'available'
        source_row['observation_count'] = len(observations)
        return {
            'status': 'available', 'observations': observations,
            'sources': [source_row], 'retrieved_at': retrieved_at,
            'completeness': 1.0, 'errors': [],
        }
    except Exception as exc:
        source_row['error'] = type(exc).__name__
        return {
            'status': 'unavailable', 'observations': [],
            'sources': [source_row], 'retrieved_at': retrieved_at,
            'completeness': 0.0, 'errors': [f'{type(exc).__name__}: {exc}'],
        }
