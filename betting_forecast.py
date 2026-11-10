"""Independent, paper-only probability baselines for football and NFL."""

import math
from collections import defaultdict


FOOTBALL_MODEL_VERSION = 'football-poisson-v1'
NFL_MODEL_VERSION = 'nfl-logistic-v1'
MIN_TEAM_MATCHES = 2
MAX_UNCERTAINTY = 0.20
MIN_EDGE = 0.02
MAX_PRICE = 4.5
PROBABILITY_FLOOR = {'football': 0.35, 'nfl': 0.45}


def _source_names(observations):
    return sorted({str(item.get('name')) for item in observations.get('sources', []) if item.get('name')})


def _no_call(version, reason, observations=None):
    observations = observations or {}
    return {
        'status': 'NO_CALL', 'model_version': version, 'validation_status': 'unvalidated',
        'probabilities': {}, 'fair_quotes': {}, 'model_probability': None,
        'fair_quote': None, 'uncertainty': 1.0,
        'source_names': _source_names(observations),
        'completeness': float(observations.get('completeness') or 0.0),
        'reason': reason,
        'rationale': f'NO_CALL · {reason}',
    }


def _usable_matches(observations):
    if not isinstance(observations, dict) or observations.get('status') != 'available':
        return None
    rows = []
    for row in observations.get('observations', []):
        if row.get('kind') != 'match_result':
            continue
        try:
            home_score = int(row['home_score'])
            away_score = int(row['away_score'])
        except (KeyError, TypeError, ValueError):
            continue
        if isinstance(row.get('home'), str) and isinstance(row.get('away'), str):
            rows.append((row['home'], row['away'], home_score, away_score))
    return rows


def _finish(version, probabilities, uncertainty, observations, rationale):
    total = sum(probabilities.values())
    probabilities = {name: value / total for name, value in probabilities.items()}
    selection = max(probabilities, key=probabilities.get)
    fair_quotes = {name: 1 / value for name, value in probabilities.items() if value > 0}
    return {
        'status': 'FORECAST', 'model_version': version, 'validation_status': 'unvalidated',
        'probabilities': {name: round(value, 8) for name, value in probabilities.items()},
        'fair_quotes': {name: round(value, 6) for name, value in fair_quotes.items()},
        'selection': selection, 'model_probability': round(probabilities[selection], 8),
        'fair_quote': round(fair_quotes[selection], 6), 'uncertainty': round(uncertainty, 6),
        'source_names': _source_names(observations),
        'completeness': float(observations.get('completeness') or 0.0),
        'rationale': rationale,
    }


def _poisson(value, goals):
    return math.exp(-value) * (value ** goals) / math.factorial(goals)


def forecast_football(event, observations):
    version = FOOTBALL_MODEL_VERSION
    rows = _usable_matches(observations)
    if rows is None:
        return _no_call(version, 'unabhängige Fußballquelle nicht verfügbar', observations)
    home_name, away_name = event.get('home'), event.get('away')
    stats = defaultdict(lambda: {'scored': [], 'conceded': []})
    for home, away, home_score, away_score in rows:
        stats[home]['scored'].append(home_score)
        stats[home]['conceded'].append(away_score)
        stats[away]['scored'].append(away_score)
        stats[away]['conceded'].append(home_score)
    if not home_name or not away_name or min(len(stats[home_name]['scored']), len(stats[away_name]['scored'])) < MIN_TEAM_MATCHES:
        return _no_call(version, 'zu wenige unabhängige Fußballspiele für beide Teams', observations)
    league_scored = sum(item['scored'][-1] for item in stats.values()) / max(1, len(stats))
    home = stats[home_name]
    away = stats[away_name]
    home_attack = sum(home['scored']) / len(home['scored'])
    away_defense = sum(away['conceded']) / len(away['conceded'])
    away_attack = sum(away['scored']) / len(away['scored'])
    home_defense = sum(home['conceded']) / len(home['conceded'])
    home_lambda = max(0.15, (home_attack + away_defense) / 2 + 0.15 + league_scored * 0.05)
    away_lambda = max(0.15, (away_attack + home_defense) / 2)
    home_win = draw = away_win = 0.0
    for home_goals in range(9):
        for away_goals in range(9):
            probability = _poisson(home_lambda, home_goals) * _poisson(away_lambda, away_goals)
            if home_goals > away_goals:
                home_win += probability
            elif home_goals == away_goals:
                draw += probability
            else:
                away_win += probability
    sample_size = min(len(home['scored']), len(away['scored']))
    uncertainty = min(0.30, 0.24 / math.sqrt(sample_size))
    return _finish(
        version,
        {home_name: home_win, 'Draw': draw, away_name: away_win},
        uncertainty,
        observations,
        f'Poisson · {sample_size} Spiele je Team · Heimvorteil 0.15 Tore',
    )


def forecast_nfl(event, observations):
    version = NFL_MODEL_VERSION
    rows = _usable_matches(observations)
    if rows is None:
        return _no_call(version, 'unabhängige NFL-Quelle nicht verfügbar', observations)
    home_name, away_name = event.get('home'), event.get('away')
    margins = defaultdict(list)
    for home, away, home_score, away_score in rows:
        margins[home].append(home_score - away_score)
        margins[away].append(away_score - home_score)
    if not home_name or not away_name or min(len(margins[home_name]), len(margins[away_name])) < MIN_TEAM_MATCHES:
        return _no_call(version, 'zu wenige unabhängige NFL-Spiele für beide Teams', observations)
    strength_delta = (sum(margins[home_name]) / len(margins[home_name])) - (sum(margins[away_name]) / len(margins[away_name])) + 1.5
    home_probability = 1 / (1 + math.exp(-strength_delta / 7.0))
    sample_size = min(len(margins[home_name]), len(margins[away_name]))
    uncertainty = min(0.30, 0.24 / math.sqrt(sample_size))
    return _finish(
        version,
        {home_name: home_probability, away_name: 1 - home_probability},
        uncertainty,
        observations,
        f'Logistic · {sample_size} Spiele je Team · Heimvorteil 1.5 Punkte',
    )


def gate_forecast(forecast, price):
    result = dict(forecast or {})
    if result.get('status') != 'FORECAST':
        result['status'] = 'NO_CALL'
        result['reason'] = result.get('reason') or 'Forecast nicht verfügbar'
        return result
    try:
        price = float(price)
        probability = float(result['model_probability'])
        uncertainty = float(result['uncertainty'])
    except (KeyError, TypeError, ValueError):
        result.update(status='NO_CALL', reason='Forecast- oder Quotenzahl ungültig')
        return result
    market = result.get('market', 'h2h')
    if market not in {'h2h', 'spreads', 'totals'}:
        result.update(status='NO_CALL', reason='Markt nicht unterstützt')
        return result
    if result.get('validation_status') != 'validated':
        result.update(status='NO_CALL', reason='Modell noch nicht out-of-sample validiert')
        return result
    floor = PROBABILITY_FLOOR.get(result.get('sport', 'football'), 0.35)
    if probability < floor:
        result.update(status='NO_CALL', reason='Gewinnwahrscheinlichkeit unter Mindestwert')
        return result
    if not math.isfinite(price) or price <= 1:
        result.update(status='NO_CALL', reason='Marktquote ungültig')
        return result
    if uncertainty > MAX_UNCERTAINTY:
        result.update(status='NO_CALL', reason='Modellunsicherheit zu hoch')
        return result
    if price > MAX_PRICE:
        result.update(status='NO_CALL', reason='Quote über konservativer Langshot-Grenze')
        return result
    adjusted_probability = max(0.0, probability - uncertainty)
    expected_value = probability * price - 1
    adjusted_expected_value = adjusted_probability * price - 1
    result.update(
        market_price=round(price, 6),
        expected_value=round(expected_value, 6),
        uncertainty_adjusted_expected_value=round(adjusted_expected_value, 6),
        edge=round(price - float(result.get('fair_quote') or (1 / probability)), 6),
    )
    if adjusted_expected_value < MIN_EDGE:
        result.update(status='NO_CALL', reason='Unsicherheitsbereinigter Vorteil zu klein')
        return result
    result.update(status='PAPER', reason='Independent forecast passed paper gates')
    return result
