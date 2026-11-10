import os
import gc
import tempfile
import unittest
import warnings
from pathlib import Path
from unittest.mock import patch

import auto_market


def candidate(**overrides):
    value = {
        'profile': 'vw_multivan_long',
        'title': 'VW Multivan T7 Lang eHybrid',
        'price_eur': '39.999 EUR',
        'mileage_km': '39.500 km',
        'year': '2025',
        'location': 'Norderstedt',
        'distance_km': '249,5',
        'image_url': 'https://example.test/car.jpg',
        'url': 'https://mobile.de/angebot/123?tracking=1',
        'source': 'mobile.de',
        'long_version_evidence': 'Lang',
        'accident_free_evidence': 'unfallfrei',
    }
    value.update(overrides)
    return value


class AutoMarketDomainTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / 'auto-market.sqlite3'
        self.env = patch.dict(os.environ, {'AUTO_MARKET_DB': str(self.db)}, clear=False)
        self.env.start()
        auto_market.auto_connect().close()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_search_profiles_define_both_long_vehicle_profiles_from_2025(self):
        self.assertEqual(
            [profile['id'] for profile in auto_market.SEARCH_PROFILES],
            ['vw_multivan_long', 'ford_tourneo_custom_long'],
        )
        for profile in auto_market.SEARCH_PROFILES:
            self.assertEqual(profile['min_year'], 2025)
            self.assertEqual(profile['max_mileage_km'], 40000)
            self.assertTrue(profile['long_version_required'])

    def test_normalize_candidate_parses_german_values_and_canonicalizes_url(self):
        listing = auto_market.normalize_candidate(candidate())

        self.assertEqual(listing['price_eur'], 39999)
        self.assertEqual(listing['mileage_km'], 39500)
        self.assertEqual(listing['distance_km'], 249.5)
        self.assertEqual(listing['url'], 'https://mobile.de/angebot/123')
        self.assertEqual(listing['source'], 'mobile.de')

    def test_normalize_candidate_rejects_missing_required_evidence(self):
        self.assertIsNone(auto_market.normalize_candidate(candidate(image_url='')))
        self.assertIsNone(auto_market.normalize_candidate(candidate(long_version_evidence='')))
        self.assertIsNone(auto_market.normalize_candidate(candidate(accident_free_evidence='')))
        self.assertIsNone(auto_market.normalize_candidate(candidate(price_eur='')))

    def test_normalize_candidate_rejects_wrong_model_for_profile(self):
        self.assertIsNone(auto_market.normalize_candidate(candidate(title='Ford Tourneo Custom lang')))

    def test_matches_filters_uses_inclusive_boundaries(self):
        listing = auto_market.normalize_candidate(candidate(price_eur='40.000 EUR', distance_km='250'))

        self.assertTrue(auto_market.matches_filters(listing, 40000, 250))
        self.assertFalse(auto_market.matches_filters(listing, 39999, 250))
        self.assertFalse(auto_market.matches_filters(listing, 40000, 249))

    def test_refresh_merges_duplicate_urls_and_favorite_survives_reload(self):
        result = auto_market.refresh_listings(lambda: [candidate(), candidate(title='Duplicate')], lambda url: None)
        self.assertEqual(result['inserted'], 1)
        listings = auto_market.list_active_listings(40000, 250)
        self.assertEqual(len(listings), 1)

        auto_market.set_favorite(listings[0]['id'], True)
        auto_market.auto_connect().close()
        self.assertTrue(auto_market.list_active_listings(40000, 250)[0]['favorite'])

    def test_refresh_preserves_transient_failure_and_removes_confirmed_404(self):
        auto_market.refresh_listings(lambda: [candidate()], lambda url: None)

        transient = auto_market.refresh_listings(lambda: [], lambda url: {'status_code': 503})
        self.assertEqual(transient['removed'], 0)
        self.assertEqual(len(auto_market.list_active_listings(40000, 250)), 1)

        gone = auto_market.refresh_listings(lambda: [], lambda url: {'status_code': 404})
        self.assertEqual(gone['removed'], 1)
        self.assertEqual(auto_market.list_active_listings(40000, 250), [])

    def test_domain_queries_close_sqlite_connections(self):
        auto_market.refresh_listings(lambda: [candidate()], lambda url: None)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always', ResourceWarning)
            auto_market.list_active_listings(40000, 250)
            auto_market.set_favorite(1, True)
            gc.collect()
        self.assertFalse(any('unclosed database' in str(item.message).lower() for item in caught))


if __name__ == '__main__':
    unittest.main()
