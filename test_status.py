import unittest
import server
import json
import threading
from http.client import HTTPConnection


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
