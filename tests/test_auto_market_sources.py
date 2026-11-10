import json
import os
import tempfile
import unittest
from base64 import urlsafe_b64encode
from pathlib import Path
from unittest.mock import patch

import auto_market_sources
import auto_market
import auto_market_refresh


LISTING_HTML = '''
<html><head>
<link rel="canonical" href="https://mobile.de/angebot/456?tracking=1">
<meta property="og:image" content="https://mobile.de/image-456.jpg">
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Car","name":"VW Multivan lang",
"vehicleModelDate":"2025","mileageFromOdometer":{"value":22000},
"offers":{"price":39900,"priceCurrency":"EUR"},
"image":"https://mobile.de/image-456.jpg",
"address":{"addressLocality":"Norderstedt","postalCode":"22844"}}
</script></head><body>Lang, unfallfrei</body></html>
'''


class FakeResponse:
    def __init__(self, body, status=200):
        self.body = body.encode() if isinstance(body, str) else body
        self.status = status

    def read(self, limit=None):
        return self.body if limit is None else self.body[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class AutoMarketSourcesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        os.environ['AUTO_MARKET_SEARCH_URL'] = 'https://search.test/api'
        os.environ['AUTO_MARKET_TRUSTED_DOMAINS'] = 'mobile.de,autoscout24.de'
        os.environ['AUTO_MARKET_DB'] = str(Path(self.temp.name) / 'auto-market.sqlite3')
        auto_market_refresh.LOCK_PATH = Path(self.temp.name) / 'refresh.lock'

    def tearDown(self):
        os.environ.pop('AUTO_MARKET_SEARCH_URL', None)
        os.environ.pop('AUTO_MARKET_TRUSTED_DOMAINS', None)
        os.environ.pop('AUTO_MARKET_DB', None)
        self.temp.cleanup()

    def test_parse_listing_html_extracts_required_fields_and_evidence(self):
        listing = auto_market_sources.parse_listing_html(LISTING_HTML, 'https://mobile.de/angebot/456')

        self.assertEqual(listing['profile'], 'vw_multivan_long')
        self.assertEqual(listing['price_eur'], 39900)
        self.assertEqual(listing['mileage_km'], 22000)
        self.assertEqual(listing['year'], 2025)
        self.assertEqual(listing['url'], 'https://mobile.de/angebot/456')
        self.assertEqual(listing['image_url'], 'https://mobile.de/image-456.jpg')
        self.assertEqual(listing['long_version_evidence'], 'Lang')
        self.assertEqual(listing['accident_free_evidence'], 'unfallfrei')

    def test_parse_listing_html_ignores_unrelated_long_text(self):
        body = LISTING_HTML.replace('"name":"VW Multivan lang"', '"name":"VW Multivan"').replace('<body>Lang, unfallfrei</body>', '<body>Besichtigung am Montag. Lang, unfallfrei</body>')
        listing = auto_market_sources.parse_listing_html(body, 'https://mobile.de/angebot/456')
        self.assertIsNone(listing['profile'])
        self.assertEqual(listing['long_version_evidence'], '')

    def test_unknown_source_is_rejected_before_fetch(self):
        self.assertFalse(auto_market_sources.is_trusted_url('https://unknown.example/car'))
        with patch.object(auto_market_sources.urllib.request, 'urlopen') as opener:
            result = auto_market_sources.fetch_listing('https://unknown.example/car')
        self.assertEqual(result['status_code'], 403)
        opener.assert_not_called()

    def test_discovery_fetches_results_for_each_profile_and_keeps_search_distance(self):
        search_body = json.dumps({'results': [{'url': 'https://mobile.de/angebot/456', 'distance_km': 42}]})
        def opener(request, timeout=0):
            target = request.full_url if hasattr(request, 'full_url') else str(request)
            return FakeResponse(search_body if 'search.test' in target else LISTING_HTML)
        with patch.object(auto_market_sources.urllib.request, 'urlopen', side_effect=opener):
            candidates = auto_market_sources.discover_candidates()
        self.assertEqual(len(candidates), 2)
        self.assertEqual(candidates[0]['distance_km'], 42)
        self.assertIn('Norderstedt', candidates[0]['location'])

    def test_discovery_uses_public_source_search_without_provider_configuration(self):
        os.environ.pop('AUTO_MARKET_SEARCH_URL', None)
        search_body = '''
        <html><body>
        <script type="application/json">{"url":"/angebote/vw-multivan-lang-456"}</script>
        </body></html>
        '''

        def opener(request, timeout=0):
            target = request.full_url if hasattr(request, 'full_url') else str(request)
            return FakeResponse(search_body if 'autoscout24.de/lst/' in target else LISTING_HTML)

        with patch.object(auto_market_sources.urllib.request, 'urlopen', side_effect=opener):
            candidates = auto_market_sources.discover_candidates()

        self.assertEqual(len(candidates), 2)

    def test_discovery_parses_html_search_results_when_configured(self):
        os.environ['AUTO_MARKET_SEARCH_URL'] = 'https://html.duckduckgo.com/html/'
        search_body = '''
        <html><body>
        <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fmobile.de%2Fangebot%2F456">VW Treffer</a>
        </body></html>
        '''

        def opener(request, timeout=0):
            target = request.full_url if hasattr(request, 'full_url') else str(request)
            return FakeResponse(search_body if 'duckduckgo.com' in target else LISTING_HTML)

        with patch.object(auto_market_sources.urllib.request, 'urlopen', side_effect=opener):
            candidates = auto_market_sources.discover_candidates()

        self.assertEqual(len(candidates), 2)
        self.assertEqual(candidates[0]['url'], 'https://mobile.de/angebot/456')

    def test_discovery_parses_bing_html_search_results(self):
        os.environ['AUTO_MARKET_SEARCH_URL'] = 'https://www.bing.com/search'
        encoded_url = urlsafe_b64encode(b'https://mobile.de/angebot/456').decode().rstrip('=')
        search_body = '''
        <html><body>
        <li class="b_algo"><h2><a href="https://www.bing.com/ck/a?u=a1%s">VW Treffer</a></h2></li>
        </body></html>
        ''' % encoded_url

        def opener(request, timeout=0):
            target = request.full_url if hasattr(request, 'full_url') else str(request)
            return FakeResponse(search_body if 'bing.com' in target else LISTING_HTML)

        with patch.object(auto_market_sources.urllib.request, 'urlopen', side_effect=opener):
            candidates = auto_market_sources.discover_candidates()

        self.assertEqual(len(candidates), 2)

    def test_malformed_provider_response_fails_closed(self):
        with patch.object(auto_market_sources.urllib.request, 'urlopen', return_value=FakeResponse('{not-json')):
            with self.assertRaises(ValueError):
                auto_market_sources.discover_candidates()

    def test_refresh_timer_has_four_local_runs_and_no_secret(self):
        timer = Path(__file__).parents[1] / 'auto-market-refresh.timer'
        service = Path(__file__).parents[1] / 'auto-market-refresh.service'
        self.assertTrue(timer.read_text().count('OnCalendar=') == 1)
        self.assertIn('00,06,12,18:00:00 Europe/Berlin', timer.read_text())
        self.assertIn('EnvironmentFile=-%h/.hermes/.env', service.read_text())
        self.assertNotIn('TOKEN', service.read_text().upper())

    def test_refresh_fixture_reports_inserted_listing(self):
        listing = {
            'profile': 'vw_multivan_long',
            'title': 'VW Multivan lang',
            'price_eur': 39000,
            'mileage_km': 20000,
            'year': 2025,
            'location': 'Norderstedt',
            'distance_km': 10,
            'image_url': 'https://mobile.de/image.jpg',
            'url': 'https://mobile.de/angebot/fixture',
            'source': 'mobile.de',
            'long_version_evidence': 'Lang',
            'accident_free_evidence': 'unfallfrei',
        }
        with patch.object(auto_market_sources, 'discover_candidates', return_value=[listing]), patch.object(auto_market_sources, 'fetch_listing', return_value=None):
            result = auto_market_refresh.run_refresh()
        self.assertEqual(result['inserted'], 1)
        self.assertEqual(result['removed'], 0)


if __name__ == '__main__':
    unittest.main()
