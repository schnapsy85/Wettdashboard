import unittest
import server
import json
import os
import sqlite3
import tempfile
import threading
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

    def test_hermes_run_fails_closed_without_fabricated_ids(self):
        payload = server.hermes_run('hello')
        self.assertEqual(payload['status'], 'unavailable')
        self.assertNotIn('id', payload)
        self.assertNotIn('session_id', payload)
        self.assertIn('Verified native task-start contract missing', payload['reason'])

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

    def test_jarvis_shell_config_runtime_contract(self):
        html = (server.ROOT / 'index.html').read_text()
        self.assertIn('data-widget="mission-control"', html)
        self.assertIn('data-widget="voice-link"', html)
        self.assertIn('data-widget="data-policy"', html)
        self.assertIn('new Set(order).forEach', html)
        self.assertIn('grid.replaceChildren(fragment)', html)
        self.assertIn('@media(max-width:640px)', html)
        self.assertIn("fetch('/api/jarvis-config')", html)

    def test_foundation_regions_have_accessible_states(self):
        html = (server.ROOT / 'index.html').read_text()
        for marker in ('Engineering System Status', 'Mission Telemetry', 'JARVIS', 'role="status"', 'UNAVAILABLE'):
            self.assertIn(marker, html)
        self.assertIn('for="dashboard-search"', html)
        self.assertIn('id="dashboard-search"', html)

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
            with sqlite3.connect(db) as con:
                con.execute('CREATE TABLE tasks (assignee TEXT, status TEXT, project_id TEXT)')
                con.executemany('INSERT INTO tasks VALUES (?, ?, ?)', [
                    ('coder', 'done', 'dashboard'),
                    ('coder', 'running', 'dashboard'),
                    ('reviewer', 'todo', None),
                ])
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
            with sqlite3.connect(db) as con:
                con.execute('CREATE TABLE tasks (assignee TEXT, status TEXT, project_id TEXT)')
                con.execute('INSERT INTO tasks VALUES (?, ?, ?)', (None, None, None))
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
