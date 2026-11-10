import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import auto_market
import server


def candidate():
    return {
        'profile': 'vw_multivan_long',
        'title': 'VW Multivan lang',
        'price_eur': 39000,
        'mileage_km': 20000,
        'year': 2025,
        'location': 'Norderstedt',
        'distance_km': 10,
        'image_url': 'https://example.test/car.jpg',
        'url': 'https://mobile.de/angebot/http-test',
        'source': 'mobile.de',
        'long_version_evidence': 'Lang',
        'accident_free_evidence': 'unfallfrei',
    }


class AutoMarketHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / 'auto-market.sqlite3'
        os.environ['AUTO_MARKET_DB'] = str(self.db)
        os.environ.pop('AUTO_MARKET_PASSWORD', None)
        auto_market.auto_connect().close()
        auto_market.refresh_listings(lambda: [candidate()], lambda url: None)
        self.httpd = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.httpd.server_address[1]}'

    def tearDown(self):
        self.httpd.shutdown()
        self.thread.join(timeout=2)
        self.httpd.server_close()
        self.temp.cleanup()

    def request(self, method, path, body=None, cookie=None, csrf=None):
        headers = {'Content-Type': 'application/json'} if body is not None else {}
        if cookie:
            headers['Cookie'] = cookie
        if csrf:
            headers['X-CSRF-Token'] = csrf
        request = Request(self.base + path, method=method, headers=headers)
        if body is not None:
            request.data = json.dumps(body).encode()
        try:
            with urlopen(request, timeout=3) as response:
                headers = dict(response.headers)
                payload = response.read()
                body = payload.decode() if headers.get('Content-Type', '').startswith('text/html') else json.loads(payload or b'{}')
                return response.status, headers, body
        except HTTPError as error:
            headers = dict(error.headers)
            payload = error.read()
            body = payload.decode(errors='replace') if headers.get('Content-Type', '').startswith('text/html') else json.loads(payload or b'{}')
            return error.code, headers, body

    @staticmethod
    def cookie(headers):
        return headers['Set-Cookie'].split(';', 1)[0]

    def page_session(self):
        status, headers, body = self.request('GET', '/auto-market')
        self.assertEqual(status, 200)
        self.assertIn('Auto Screener', body)
        self.assertIn('Set-Cookie', headers)
        return self.cookie(headers)

    def test_state_is_available_without_separate_login_and_page_sets_csrf_cookie(self):
        cookie = self.page_session()
        status, _, state = self.request('GET', '/api/auto-market/state', cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(len(state['profiles']), 2)
        self.assertTrue(state['csrf'])

        status, _, body = self.request('POST', '/api/auto-market/login', {})
        self.assertEqual(status, 404)
        self.assertEqual(body['error'], 'Nicht gefunden')

    def test_state_returns_profiles_and_editable_filters(self):
        cookie = self.page_session()
        status, _, state = self.request('GET', '/api/auto-market/state?max_price_eur=39999&max_distance_km=200', cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(state['filters'], {'max_price_eur': 39999, 'max_distance_km': 200})
        self.assertEqual([profile['id'] for profile in state['profiles']], ['vw_multivan_long', 'ford_tourneo_custom_long'])
        self.assertEqual(len(state['listings']), 1)

    def test_favorite_requires_csrf_and_survives_a_fresh_page_session(self):
        cookie = self.page_session()
        status, _, state = self.request('GET', '/api/auto-market/state', cookie=cookie)
        self.assertEqual(status, 200)
        listing_id = state['listings'][0]['id']
        csrf = state['csrf']

        status, _, _ = self.request('POST', '/api/auto-market/favorite', {'listing_id': listing_id, 'favorite': True}, cookie=cookie)
        self.assertEqual(status, 403)
        status, _, body = self.request('POST', '/api/auto-market/favorite', {'listing_id': listing_id, 'favorite': True}, cookie=cookie, csrf=csrf)
        self.assertEqual(status, 200)
        self.assertTrue(body['favorite'])

        fresh_cookie = self.page_session()
        status, _, fresh_state = self.request('GET', '/api/auto-market/state', cookie=fresh_cookie)
        self.assertEqual(status, 200)
        self.assertTrue(fresh_state['listings'][0]['favorite'])

    def test_page_is_served_with_both_profile_labels(self):
        status, _, body = self.request('GET', '/auto-market.html')
        self.assertEqual(status, 200)
        self.assertIn('Auto Screener', body if isinstance(body, str) else '')
        self.assertNotIn('Dashboard-Zugang', body if isinstance(body, str) else '')
        self.assertNotIn('id="password"', body if isinstance(body, str) else '')


if __name__ == '__main__':
    unittest.main()
