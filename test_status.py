import unittest
import server
import json
import os
import sqlite3
import tempfile
import threading
from contextlib import closing
from pathlib import Path
from http.client import HTTPConnection
from unittest.mock import patch


class StatusTests(unittest.TestCase):
    def test_load_local_env_uses_custom_hermes_home(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / '.env').write_text('DASHBOARD_TEST_LOCAL_ENV=loaded\n')
            with patch.dict(os.environ, {'HERMES_HOME': directory}, clear=False):
                os.environ.pop('DASHBOARD_TEST_LOCAL_ENV', None)
                server.load_local_env()
                self.assertEqual(os.environ.get('DASHBOARD_TEST_LOCAL_ENV'), 'loaded')
                os.environ.pop('DASHBOARD_TEST_LOCAL_ENV', None)

    def test_engineering_status_has_stable_read_only_shape(self):
        payload = server.engineering_status()
        self.assertEqual(set(payload), {'overall', 'retrieved_at', 'sources'})
        self.assertIn(payload['overall'], {'AVAILABLE', 'UNAVAILABLE'})
        self.assertIsInstance(payload['sources'], list)
        self.assertEqual({item['name'] for item in payload['sources']}, {'Hermes gateway', 'Nine Router', 'Dashboard'})
        for item in payload['sources']:
            self.assertEqual(set(item), {'name', 'status', 'value', 'source', 'error'})
            self.assertIn(item['status'], {'AVAILABLE', 'UNAVAILABLE'})

    @classmethod
    def setUpClass(cls):
        cls.httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.httpd.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.thread.join()
        cls.httpd.server_close()

    def request(self, method, path, body=None, headers=None):
        conn = HTTPConnection('127.0.0.1', self.port)
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        body = response.read()
        conn.close()
        return response.status, body

    def test_conversation_uses_real_router_or_fails_closed(self):
        status, body = self.request('POST', '/api/conversation', json.dumps({'message': 'hello'}), {'Content-Type': 'application/json'})
        payload = json.loads(body)
        self.assertIn(status, {200, 503})
        if status == 200:
            self.assertIsInstance(payload.get('response'), str)
            self.assertTrue(payload['response'].strip())
        else:
            self.assertIn('error', payload)
            self.assertNotIn('response', payload)
        self.assertNotIn('fake', json.dumps(payload).lower())

    def test_hermes_run_uses_native_cli_and_real_status(self):
        responses = [
            ({'id': 'task-real'}, None),
            ({'status': 'ok'}, None),
            ({'id': 'task-real', 'status': 'done', 'current_run_id': 42,
              'result': 'completed', 'latest_summary': 'worker finished'}, None),
            ({'id': 'task-real', 'status': 'done', 'current_run_id': 42,
              'result': 'completed', 'latest_summary': 'worker finished'}, None),
        ]
        def native(args):
            if args == ['dispatch']:
                return {'status': 'ok'}, None
            return responses.pop(0)
        with patch.dict(os.environ, {'HERMES_HOME': '/tmp/hermes-test'}), \
             patch.object(server, '_native_task_cli', side_effect=native) as cli:
            payload = server.hermes_run('hello')
        self.assertEqual(payload, {'status': 'done', 'task_id': 'task-real', 'run_id': 42,
                                   'session_id': None, 'result': 'completed', 'latest_summary': 'worker finished'})
        self.assertEqual(cli.call_args_list[0].args[0][:2], ['create', 'Dashboard order'])
        self.assertIn('--idempotency-key', cli.call_args_list[0].args[0])
        self.assertEqual(cli.call_args_list[2].args[0], ['show', 'task-real'])

    def test_hermes_run_fails_closed_without_executable(self):
        with patch.object(server, '_hermes_executable', return_value=None):
            payload = server.hermes_run('hello')
        self.assertEqual(payload['status'], 'unavailable')
        self.assertIn('executable unavailable', payload['reason'])

    def test_native_cli_explicitly_routes_profile_and_root_home(self):
        completed = type('Completed', (), {'returncode': 0, 'stdout': '{"status":"ok"}', 'stderr': ''})()
        with patch.object(server, '_hermes_executable', return_value='/verified/hermes'), \
             patch.object(server.subprocess, 'run', return_value=completed) as run:
            payload, error = server._native_task_cli(['show', 'task'])
        self.assertEqual((payload, error), ({'status': 'ok'}, None))
        env = run.call_args.kwargs['env']
        self.assertEqual(env['HERMES_HOME'], str(server.hermes_home()))
        self.assertEqual(env['HERMES_PROFILE'], os.environ.get('HERMES_PROFILE', 'coder'))

    def test_hermes_poll_requires_dashboard_task_owner(self):
        with patch.object(server, '_native_task_cli', return_value=({'status': 'done'}, None)):
            status, body = self.request('GET', '/api/hermes/run?task_id=arbitrary')
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body), {'error': 'Task-Zugriff verweigert'})

    def test_native_task_ui_has_one_bound_form_and_no_stale_blocker(self):
        html = (server.ROOT / 'index.html').read_text()
        self.assertEqual(html.count('id="hermes-run-form"'), 1)
        self.assertEqual(html.count('id="hermes-run-input"'), 1)
        self.assertEqual(html.count('id="hermes-run-status"'), 1)
        self.assertIn('NATIVE CLI &middot; FAIL CLOSED', html)
        self.assertNotIn('verified task-start contract missing', html)

    def test_targeted_function_matrix_safe_contracts(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('THE_ODDS_API_KEY', None)
            odds = server.odds_feed()
        self.assertEqual(odds['status'], 'unavailable')
        self.assertEqual(odds['events'], [])
        self.assertEqual(server.hyperliquid_bot.config()['environment'], 'testnet')
        self.assertIn('signals', server.hyperliquid_bot.strategy_contract())
        tokens = server.router_stats()
        self.assertIn(tokens.get('usage'), {'AVAILABLE', 'UNAVAILABLE'})
        fantasy = server.fantasy_data({'season': '2026', 'week': '4'})
        self.assertIn('status', fantasy) if 'status' in fantasy else self.assertEqual(fantasy.get('error'), 'Fantasy Backend nicht erreichbar')

    def test_paper_model_finds_line_shopping_candidate_without_calling_it_profit(self):
        markets = []
        for bookmaker, home_price in (('A', 2.00), ('B', 2.05), ('C', 2.10), ('D', 2.20)):
            markets.append({'bookmaker': bookmaker, 'market': 'h2h', 'outcomes': [
                {'name': 'Home', 'price': home_price}, {'name': 'Away', 'price': 2.00}
            ]})
        result = server.baseline_model({'markets': markets})
        self.assertEqual(result['status'], 'PAPER')
        self.assertEqual(result['best_bookmaker'], 'D')
        self.assertGreater(result['expected_value'], 0)
        self.assertIn('PAPER ONLY', result['rationale'])

    def test_independent_source_contract(self):
        import betting_sources
        from datetime import datetime, timezone

        now = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
        event = {
            'id': 'evt-1', 'competition': 'Bundesliga', 'home': 'Home FC',
            'away': 'Away FC', 'start': '2026-10-10T15:30:00Z',
        }
        match = [{
            'MatchID': 7, 'MatchDateTime': '2026-10-01T15:30:00',
            'Team1': {'TeamName': 'Home FC'}, 'Team2': {'TeamName': 'Away FC'},
            'MatchResults': [{'PointsTeam1': 2, 'PointsTeam2': 1}],
        }]

        def good_fetch(url, timeout, max_bytes):
            self.assertIn('openligadb', url)
            self.assertEqual(timeout, 10)
            self.assertGreater(max_bytes, 1000)
            return match

        result = betting_sources.source_snapshot(event, 'football', now, fetch_json=good_fetch)
        self.assertEqual(result['status'], 'available')
        self.assertEqual(result['completeness'], 1.0)
        self.assertEqual(result['observations'][0]['kind'], 'match_result')
        self.assertEqual(result['observations'][0]['home_score'], 2)
        self.assertEqual(result['sources'][0]['name'], 'OpenLigaDB')
        self.assertTrue(result['sources'][0]['payload_hash'])
        self.assertIn('retrieved_at', result)

        camel_case_match = [{
            'matchID': 8, 'matchDateTimeUTC': '2026-10-01T15:30:00Z', 'matchIsFinished': True,
            'team1': {'teamName': 'Home FC'}, 'team2': {'teamName': 'Away FC'},
            'matchResults': [{'pointsTeam1': 2, 'pointsTeam2': 1, 'resultTypeKind': 'After90Minutes'}],
        }]
        camel_result = betting_sources.source_snapshot(event, 'football', now, fetch_json=lambda *_args: camel_case_match)
        self.assertEqual(camel_result['status'], 'available')
        self.assertEqual(camel_result['observations'][0]['home_score'], 2)
        self.assertGreaterEqual(betting_sources.MAX_RESPONSE_BYTES, 8_000_000)

        zero_score_match = [{
            'MatchID': 9, 'MatchDateTime': '2026-10-02T15:30:00Z', 'MatchIsFinished': True,
            'Team1': {'TeamName': 'Home FC'}, 'Team2': {'TeamName': 'Away FC'},
            'MatchResults': [{'PointsTeam1': 0, 'PointsTeam2': 0}],
        }]
        zero_result = betting_sources.source_snapshot(event, 'football', now, fetch_json=lambda *_args: zero_score_match)
        self.assertEqual(zero_result['status'], 'available')
        self.assertEqual(zero_result['observations'][0]['home_score'], 0)

        malformed = betting_sources.source_snapshot(event, 'football', now, fetch_json=lambda *_args: {})
        self.assertEqual(malformed['status'], 'unavailable')
        self.assertIn('malformed', ' '.join(malformed['errors']).lower())

        stale = betting_sources.source_snapshot(
            event, 'football', now,
            fetch_json=lambda *_args: [{**match[0], 'MatchDateTime': '2020-01-01T00:00:00'}],
        )
        self.assertEqual(stale['status'], 'unavailable')
        self.assertIn('stale', ' '.join(stale['errors']).lower())

        failed = betting_sources.source_snapshot(
            event, 'football', now,
            fetch_json=lambda *_args: (_ for _ in ()).throw(TimeoutError('timeout')),
        )
        self.assertEqual(failed['status'], 'unavailable')
        self.assertIn('TimeoutError', ' '.join(failed['errors']))

    def test_independent_forecast_contract(self):
        import betting_forecast

        football_event = {'home': 'Home FC', 'away': 'Away FC', 'competition': 'Bundesliga'}
        football_data = {
            'status': 'available', 'completeness': 1.0,
            'sources': [{'name': 'OpenLigaDB', 'payload_hash': 'a'}],
            'observations': [
                {'kind': 'match_result', 'home': 'Home FC', 'away': 'North FC', 'home_score': 2, 'away_score': 0, 'observed_at': '2026-09-20T12:00:00+00:00'},
                {'kind': 'match_result', 'home': 'North FC', 'away': 'Home FC', 'home_score': 1, 'away_score': 1, 'observed_at': '2026-09-13T12:00:00+00:00'},
                {'kind': 'match_result', 'home': 'Away FC', 'away': 'South FC', 'home_score': 0, 'away_score': 2, 'observed_at': '2026-09-20T12:00:00+00:00'},
                {'kind': 'match_result', 'home': 'South FC', 'away': 'Away FC', 'home_score': 1, 'away_score': 0, 'observed_at': '2026-09-13T12:00:00+00:00'},
            ],
        }
        football = betting_forecast.forecast_football(football_event, football_data)
        self.assertEqual(football['status'], 'FORECAST')
        self.assertAlmostEqual(sum(football['probabilities'].values()), 1.0, places=6)
        self.assertEqual(football['source_names'], ['OpenLigaDB'])

        nfl_event = {'home': 'HOME', 'away': 'AWAY', 'competition': 'NFL'}
        nfl_data = {
            'status': 'available', 'completeness': 1.0,
            'sources': [{'name': 'nflverse', 'payload_hash': 'b'}],
            'observations': [
                {'kind': 'match_result', 'home': 'HOME', 'away': 'X', 'home_score': 24, 'away_score': 14, 'observed_at': '2026-09-20T12:00:00+00:00'},
                {'kind': 'match_result', 'home': 'X', 'away': 'HOME', 'home_score': 17, 'away_score': 20, 'observed_at': '2026-09-13T12:00:00+00:00'},
                {'kind': 'match_result', 'home': 'AWAY', 'away': 'Y', 'home_score': 14, 'away_score': 24, 'observed_at': '2026-09-20T12:00:00+00:00'},
                {'kind': 'match_result', 'home': 'Y', 'away': 'AWAY', 'home_score': 21, 'away_score': 17, 'observed_at': '2026-09-13T12:00:00+00:00'},
            ],
        }
        nfl = betting_forecast.forecast_nfl(nfl_event, nfl_data)
        self.assertEqual(nfl['status'], 'FORECAST')
        self.assertAlmostEqual(sum(nfl['probabilities'].values()), 1.0, places=6)

        validated = {**football, 'validation_status': 'validated'}
        at_two = betting_forecast.gate_forecast(validated, 2.0)
        at_three = betting_forecast.gate_forecast(validated, 3.0)
        self.assertEqual(at_two['model_probability'], at_three['model_probability'])

        missing = betting_forecast.forecast_nfl(nfl_event, {'status': 'unavailable', 'observations': [], 'sources': [], 'completeness': 0.0})
        self.assertEqual(missing['status'], 'NO_CALL')
        self.assertIn('quelle', missing['reason'].lower())

        outsider = betting_forecast.gate_forecast({
            'status': 'FORECAST', 'validation_status': 'validated', 'model_version': 'test-v1',
            'model_probability': 0.20, 'uncertainty': 0.04, 'source_names': ['test'],
        }, 6.0)
        self.assertEqual(outsider['status'], 'NO_CALL')
        self.assertIn('wahrscheinlichkeit', outsider['reason'].lower())

    def test_forecast_persistence_contract(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(server, 'BETTING_DB', Path(directory) / 'betting.sqlite3'):
            snapshot = server.save_betting_snapshot({'status': 'available', 'source': 'test', 'events': []})
            feature = server.save_feature_snapshot(
                snapshot['id'], 'evt-1', 'football',
                {'status': 'available', 'completeness': 1.0, 'observations': [{'kind': 'match_result'}], 'sources': [{'name': 'OpenLigaDB', 'payload_hash': 'abc'}]},
            )
            self.assertGreater(feature['id'], 0)
            model = server.save_model_run(
                snapshot['id'], 'evt-1', 'football',
                {'model_version': 'football-poisson-v1', 'validation_status': 'unvalidated', 'rationale': 'fixture'},
            )
            self.assertEqual(model['model_version'], 'football-poisson-v1')
            forecast_payload = {
                'status': 'NO_CALL', 'validation_status': 'unvalidated', 'model_version': 'football-poisson-v1',
                'model_probability': 0.40, 'fair_quote': 2.5, 'market_price': 2.7,
                'uncertainty': 0.15, 'expected_value': 0.08, 'reason': 'not validated',
                'source_names': ['OpenLigaDB'],
            }
            first = server.save_forecast(snapshot['id'], 'evt-1', 'football', 'h2h', 'Home FC', forecast_payload)
            second = server.save_forecast(snapshot['id'], 'evt-1', 'football', 'h2h', 'Home FC', forecast_payload)
            self.assertEqual(first['id'], second['id'])
            rows = server.latest_forecasts(snapshot['id'])
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['source_names'], ['OpenLigaDB'])
            metrics = server.forecast_metrics()
            self.assertEqual(metrics['status'], 'UNAVAILABLE')
            self.assertEqual(metrics['sample_size'], 0)

    def test_forecast_refresh_contract(self):
        import betting_forecast
        import betting_sources

        features = {
            'status': 'available', 'completeness': 1.0,
            'observations': [{'kind': 'match_result'}],
            'sources': [{'name': 'OpenLigaDB', 'payload_hash': 'abc'}],
            'retrieved_at': '2026-10-06T12:00:00+00:00', 'errors': [],
        }
        event = {
            'id': 'evt-1', 'sport': 'soccer', 'competition': 'Bundesliga',
            'home': 'Home FC', 'away': 'Away FC', 'start': '2026-10-10T15:30:00Z',
            'markets': [{'bookmaker': 'Book', 'market': 'h2h', 'outcomes': [
                {'name': 'Home FC', 'price': 2.0}, {'name': 'Away FC', 'price': 2.5},
            ]}],
        }
        feed = {'status': 'available', 'source': 'test', 'retrieved_at': '2026-10-06T12:00:00+00:00', 'events': [event]}
        model = {
            'status': 'FORECAST', 'model_version': 'football-poisson-v1', 'validation_status': 'validated',
            'probabilities': {'Home FC': 0.58, 'Away FC': 0.42},
            'fair_quotes': {'Home FC': 1.7241, 'Away FC': 2.381}, 'uncertainty': 0.05,
            'source_names': ['OpenLigaDB'], 'completeness': 1.0, 'rationale': 'fixture',
        }
        real_forecast = betting_forecast.forecast_football
        def forecast_for_features(current_event, current_features):
            return model if current_features.get('status') == 'available' else real_forecast(current_event, current_features)
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(server, 'BETTING_DB', Path(directory) / 'betting.sqlite3'), \
             patch.object(server, 'fetch_odds_feed', return_value=feed), \
             patch.object(server, 'save_betting_snapshot', side_effect=[
                 {'id': 7, 'retrieved_at': feed['retrieved_at'], 'event_count': 1},
                 {'id': 7, 'retrieved_at': feed['retrieved_at'], 'event_count': 1},
                 {'id': 8, 'retrieved_at': feed['retrieved_at'], 'event_count': 1},
             ]), \
             patch.object(betting_sources, 'source_snapshot', return_value=features), \
             patch.object(betting_forecast, 'forecast_football', side_effect=forecast_for_features):
            first = server.refresh_betting_snapshot()
            second = server.refresh_betting_snapshot()
            self.assertEqual(first['forecast_status'], 'available')
            self.assertEqual(first['forecast_candidates'], 1)
            self.assertEqual(len(server.latest_forecasts(7)), 2)
            self.assertEqual(second['forecast_candidates'], 1)
            self.assertEqual(len(server.latest_forecasts(7)), 2)

            patcher = patch.object(betting_sources, 'source_snapshot', return_value={
                'status': 'unavailable', 'completeness': 0.0, 'observations': [],
                'sources': [{'name': 'OpenLigaDB', 'status': 'unavailable'}],
                'retrieved_at': feed['retrieved_at'], 'errors': ['TimeoutError: timeout'],
            })
            with patcher:
                failed = server.refresh_betting_snapshot()
            self.assertEqual(failed['forecast_status'], 'unavailable')
            self.assertEqual(failed['forecast_candidates'], 0)
            self.assertIn('TimeoutError', failed['forecast_errors'][0])
            failed_rows = server.latest_forecasts(8)
            self.assertEqual(len(failed_rows), 1)
            self.assertEqual(failed_rows[0]['status'], 'NO_CALL')
            self.assertEqual(failed_rows[0]['selection'], 'UNAVAILABLE')

    def test_independent_forecast_ui_contract(self):
        html = (server.ROOT / 'betting.html').read_text()
        script = (server.ROOT / 'betting.js').read_text()
        combined = html + script
        for marker in ('Eigene Prognose', 'Gewinnchance', 'Unsicherheit', 'Datenstand', 'model_version', 'NO_CALL'):
            self.assertIn(marker, combined)
        self.assertNotIn('Marktbasierte Kandidaten', html)
        self.assertNotIn('Kein unabhängiges Vorhersagemodell', combined)
        self.assertIn('Keine Echtgeldabgabe', html)
        self.assertNotIn('Echtgeld platzieren', combined)

    def test_betting_snapshot_roundtrip_is_persistent(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(server, 'BETTING_DB', Path(directory) / 'betting.sqlite3'):
            saved = server.save_betting_snapshot({'status': 'available', 'source': 'test', 'events': []})
            loaded = server.latest_betting_snapshot()
        self.assertEqual(loaded['id'], saved['id'])
        self.assertEqual(loaded['events'], [])

    def test_paper_betting_state_and_write_protection(self):
        status, body = self.request('GET', '/api/betting/state')
        self.assertEqual(status, 200)
        self.assertEqual({'feed', 'candidates', 'paper_bets', 'metrics'}, set(json.loads(body)))
        status, body = self.request('POST', '/api/betting/paper-bets', json.dumps({}), {'Content-Type': 'application/json'})
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body), {'error': 'CSRF-Prüfung fehlgeschlagen'})

    def test_paper_journal_settlement_updates_metrics(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(server, 'BETTING_DB', Path(directory) / 'betting.sqlite3'), patch.dict(os.environ, {'THE_ODDS_API_KEY': 'test-key'}):
            saved = server.save_betting_snapshot({'status': 'available', 'source': 'test', 'events': []})
            bet = server.save_paper_bet({'snapshot_id': saved['id'], 'event_id': 'event-1', 'market': 'h2h', 'selection': 'Home', 'bookmaker': 'Testbook', 'odds': 2.0, 'model_probability': 0.55, 'expected_value': 0.10})
            self.assertEqual(server.betting_state()['metrics']['open_bets'], 1)
            server.settle_paper_bet(bet['id'], 'win')
            state = server.betting_state()
        self.assertEqual(state['metrics']['settled_bets'], 1)
        self.assertEqual(state['metrics']['profit_cents'], 250)
        self.assertEqual(state['metrics']['roi'], 1.0)

    def test_targeted_function_matrix_fixture_and_route_contracts(self):
        rows = server.finance_parse_csv(
            b'Datum;Beschreibung;Betrag\n31.12.2025;Supermarkt;-12,50\n', 'checking')
        self.assertEqual(rows[0]['amount_cents'], -1250)
        self.assertEqual(rows[0]['kind'], 'expense')
        self.assertEqual(server.hyperliquid_bot.config()['environment'], 'testnet')
        html = (server.ROOT / 'index.html').read_text()
        for href in ('/fantasy', '/finanzen.html', '/trading', '/betting', '/tokens.html'):
            self.assertIn(f'href="{href}"', html)
        betting = (server.ROOT / 'betting.html').read_text()
        self.assertIn('id="sport-filter"', betting)
        self.assertIn('id="competition-filter"', betting)
        self.assertIn('Paper-Wette', betting)
        self.assertIn('/api/betting/state', betting)
        tokens = (server.ROOT / 'tokens.html').read_text()
        self.assertIn('/api/tokens', tokens)
        self.assertIn('usage', tokens)

    def test_conversation_rejects_invalid_and_oversize_input(self):
        for body in (b'{', json.dumps({'message': 'x' * (server.CONVERSATION_MAX_INPUT + 1)}).encode()):
            status, _ = self.request('POST', '/api/conversation', body, {'Content-Type': 'application/json'})
            self.assertEqual(status, 400)

    def test_jarvis_config_defaults_and_http_contract(self):
        payload = server.jarvis_config()
        self.assertEqual(payload['source'], 'defaults')
        self.assertEqual(payload['layout'], ['mission-control', 'voice-link', 'data-policy'])
        status, body = self.request('GET', '/api/jarvis-config')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['theme']['cyan'], '#09d6ff')

    def test_compact_command_center_runtime_contract(self):
        html = (server.ROOT / 'index.html').read_text()
        for marker in (
            'id="hermes-run-form"',
            'id="engineering-status"',
            'id="telemetry"',
            'id="agents-summary"',
            'id="token-status"',
            'id="system-status"',
            'id="service-status"',
            'align-items:start',
            '@media(max-width:640px)',
        ):
            self.assertIn(marker, html)
        for obsolete in (
            'JUST A RATHER VERY INTELLIGENT SYSTEM',
            'class="jarvis-shell"',
            'id="conversation-form"',
            'Antwort vorlesen',
            'Gesamtleistung',
            'Verbundene Bereiche',
            'class="svc',
            "fetch('/api/jarvis-config')",
        ):
            self.assertNotIn(obsolete, html)

    def test_foundation_regions_have_accessible_states(self):
        html = (server.ROOT / 'index.html').read_text()
        for marker in ('Engineering System Status', 'Mission Telemetry', 'role="status"', 'UNAVAILABLE'):
            self.assertIn(marker, html)

    def test_jarvis_config_custom_order_is_preserved(self):
        config = server.ROOT / 'jarvis.config.json'
        original = config.read_text() if config.exists() else None
        try:
            config.write_text(json.dumps({'layout': ['data-policy', 'mission-control', 'voice-link']}))
            payload = server.jarvis_config()
            self.assertEqual(payload['layout'], ['data-policy', 'mission-control', 'voice-link'])
        finally:
            if original is None:
                config.unlink(missing_ok=True)
            else:
                config.write_text(original)

    def test_telemetry_is_read_only_and_bounded(self):
        payload = server.telemetry()
        self.assertEqual(set(payload), {'status', 'source', 'agents', 'tasks', 'projects'})
        self.assertIn(payload['status'], {'AVAILABLE', 'UNAVAILABLE'})
        self.assertIsInstance(payload['agents'], list)

    def test_telemetry_fails_closed_when_source_unavailable(self):
        with patch('server.sqlite3.connect', side_effect=server.sqlite3.OperationalError):
            payload = server.telemetry()
        self.assertEqual(payload['status'], 'UNAVAILABLE')
        self.assertEqual(payload['agents'], [])
        self.assertEqual(payload['tasks'], {})
        self.assertEqual(payload['projects'], {})

    def test_telemetry_maps_real_source_without_fabricating_progress(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / '.hermes' / 'kanban.db'
            db.parent.mkdir()
            with closing(sqlite3.connect(db)) as con:
                con.execute('CREATE TABLE tasks (assignee TEXT, status TEXT, project_id TEXT)')
                con.executemany('INSERT INTO tasks VALUES (?, ?, ?)', [
                    ('coder', 'done', 'dashboard'),
                    ('coder', 'running', 'dashboard'),
                    ('reviewer', 'todo', None),
                ])
                con.commit()
            with patch.dict(os.environ, {'HERMES_HOME': str(Path(directory) / '.hermes')}, clear=False):
                payload = server.telemetry()
        self.assertEqual(payload['status'], 'AVAILABLE')
        self.assertEqual(payload['tasks'], {'done': 1, 'running': 1, 'todo': 1})
        self.assertEqual(payload['projects'], {'UNAVAILABLE': 1, 'dashboard': 2})
        self.assertEqual(payload['agents'], [
            {'name': 'coder', 'tasks': {'done': 1, 'running': 1}},
            {'name': 'reviewer', 'tasks': {'todo': 1}},
        ])
        self.assertNotIn('progress', payload)
        self.assertNotIn('events', payload)

    def test_telemetry_marks_missing_agent_and_status_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / '.hermes' / 'kanban.db'
            db.parent.mkdir()
            with closing(sqlite3.connect(db)) as con:
                con.execute('CREATE TABLE tasks (assignee TEXT, status TEXT, project_id TEXT)')
                con.execute('INSERT INTO tasks VALUES (?, ?, ?)', (None, None, None))
                con.commit()
            with patch.dict(os.environ, {'HERMES_HOME': str(Path(directory) / '.hermes')}, clear=False):
                payload = server.telemetry()
        self.assertEqual(payload['agents'], [{'name': 'UNAVAILABLE', 'tasks': {'UNAVAILABLE': 1}}])
        self.assertEqual(payload['tasks'], {'UNAVAILABLE': 1})
        self.assertEqual(payload['projects'], {'UNAVAILABLE': 1})

    def test_telemetry_fails_closed_for_missing_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / '.hermes' / 'kanban.db'
            db.parent.mkdir()
            sqlite3.connect(db).close()
            with patch('server.Path.home', return_value=Path(directory)):
                payload = server.telemetry()
        self.assertEqual(payload['status'], 'UNAVAILABLE')
        self.assertEqual(payload['agents'], [])
        self.assertEqual(payload['tasks'], {})
        self.assertEqual(payload['projects'], {})

    def test_telemetry_does_not_create_missing_source(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / '.hermes' / 'kanban.db'
            db.parent.mkdir()
            with patch('server.Path.home', return_value=Path(directory)):
                payload = server.telemetry()
        self.assertEqual(payload['status'], 'UNAVAILABLE')
        self.assertFalse(db.exists())

    def test_telemetry_endpoint_rejects_post(self):
        status, body = self.request('POST', '/api/telemetry')
        self.assertEqual(status, 405)
        self.assertEqual(json.loads(body), {'error': 'Method not allowed'})

    def test_status_endpoint_get_query_and_existing_route(self):
        status, body = self.request('GET', '/api/engineering-status?check=1')
        self.assertEqual(status, 200)
        self.assertEqual(set(json.loads(body)), {'overall', 'retrieved_at', 'sources'})
        status, body = self.request('GET', '/')
        self.assertEqual(status, 200)
        self.assertIn(b'<!doctype html>', body.lower())

    def test_status_endpoint_rejects_post(self):
        status, body = self.request('POST', '/api/engineering-status?check=1')
        self.assertEqual(status, 405)
        self.assertEqual(json.loads(body), {'error': 'Method not allowed'})

    def test_status_endpoint_post_allows_get_only(self):
        conn = HTTPConnection('127.0.0.1', self.port)
        conn.request('POST', '/api/engineering-status')
        response = conn.getresponse()
        response.read()
        self.assertEqual(response.getheader('Allow'), 'GET')
        conn.close()


if __name__ == '__main__':
    unittest.main()
