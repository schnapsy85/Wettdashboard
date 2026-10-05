import unittest
import server
import json
import sqlite3
import tempfile
import threading
from pathlib import Path
from http.client import HTTPConnection
from unittest.mock import patch


class StatusTests(unittest.TestCase):
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

    def request(self, method, path):
        conn = HTTPConnection('127.0.0.1', self.port)
        conn.request(method, path)
        response = conn.getresponse()
        body = response.read()
        conn.close()
        return response.status, body

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
            with patch('server.Path.home', return_value=Path(directory)):
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
            with patch('server.Path.home', return_value=Path(directory)):
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
