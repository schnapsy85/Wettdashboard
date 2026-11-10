import html
import json
import math
import os
import re
import urllib.error
import urllib.request
from base64 import urlsafe_b64decode
from functools import lru_cache
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlencode, urlsplit

import auto_market


DEFAULT_TRUSTED_DOMAINS = ('mobile.de', 'autoscout24.de', 'hey.car', 'autouncle.de', 'autohero.com', 'kleinanzeigen.de')
DEFAULT_SEARCH_URL = 'https://www.bing.com/search'
DEFAULT_SOURCE_SEARCH_URLS = {
    'vw_multivan_long': 'https://www.autoscout24.de/lst/volkswagen/multivan/ge_t7/re_2025',
    'ford_tourneo_custom_long': 'https://www.autoscout24.de/lst/ford/tourneo-custom/re_2025',
}
DEFAULT_GEOCODER_URL = 'https://nominatim.openstreetmap.org/search'
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


def trusted_domains():
    configured = os.environ.get('AUTO_MARKET_TRUSTED_DOMAINS', '')
    return tuple(item.strip().lower() for item in configured.split(',') if item.strip()) or DEFAULT_TRUSTED_DOMAINS


def is_trusted_url(url):
    try:
        hostname = (urlsplit(url).hostname or '').lower().rstrip('.')
    except ValueError:
        return False
    return bool(hostname) and any(hostname == domain or hostname.endswith('.' + domain) for domain in trusted_domains())


class _ListingParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.json_ld = []
        self.meta = {}
        self.canonical = None
        self.title = []
        self.script_type = None
        self.script_data = []
        self.visible = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == 'link' and values.get('rel', '').lower() == 'canonical':
            self.canonical = values.get('href')
        if tag == 'meta' and values.get('property') in ('og:image', 'og:title'):
            self.meta[values['property']] = values.get('content', '')
        if tag == 'script':
            self.script_type = values.get('type', '').lower()
            self.script_data = []

    def handle_endtag(self, tag):
        if tag == 'script':
            if self.script_type == 'application/ld+json':
                self.json_ld.append(''.join(self.script_data))
            self.script_type = None
            self.script_data = []

    def handle_data(self, data):
        if self.script_type:
            self.script_data.append(data)
        else:
            self.visible.append(data)


class _SearchResultParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.urls = []
        self.in_bing_result = False

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        classes = values.get('class', '').split()
        if tag == 'li' and 'b_algo' in classes:
            self.in_bing_result = True
            return
        if tag != 'a':
            return
        href = html.unescape(values.get('href', ''))
        is_autoscout_result = href.startswith('/angebote/')
        if 'result__a' not in classes and not self.in_bing_result and not is_autoscout_result:
            return
        if is_autoscout_result:
            href = 'https://www.autoscout24.de' + href
        if href.startswith('//'):
            href = 'https:' + href
        parsed = urlsplit(href)
        target = parse_qs(parsed.query).get('uddg', [href])[0]
        if parsed.hostname == 'www.bing.com' and parsed.path == '/ck/a':
            encoded = parse_qs(parsed.query).get('u', [''])[0]
            if encoded.startswith('a1'):
                try:
                    padded = encoded[2:] + '=' * (-len(encoded[2:]) % 4)
                    target = urlsafe_b64decode(padded).decode('utf-8')
                except (UnicodeDecodeError, ValueError):
                    target = ''
        if target.startswith(('http://', 'https://')):
            if target not in self.urls:
                self.urls.append(target)

    def handle_endtag(self, tag):
        if tag == 'li' and self.in_bing_result:
            self.in_bing_result = False


def _walk_json(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


def _structured_documents(parser):
    documents = []
    for raw in parser.json_ld:
        try:
            documents.append(json.loads(raw))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    return [item for document in documents for item in _walk_json(document)]


def _first(documents, *keys):
    for document in documents:
        for key in keys:
            if document.get(key) not in (None, ''):
                return document[key]
    return None


def _nested_text(value):
    if isinstance(value, dict):
        return ' '.join(_nested_text(item) for item in value.values())
    if isinstance(value, list):
        return ' '.join(_nested_text(item) for item in value)
    return str(value or '')


def _vehicle_documents(documents):
    vehicle_types = {'car', 'vehicle', 'product'}
    selected = []
    for document in documents:
        raw_types = document.get('@type')
        types = raw_types if isinstance(raw_types, list) else [raw_types]
        normalized = {str(item).lower() for item in types if item}
        if normalized & vehicle_types or any(key in document for key in ('vehicleModelDate', 'mileageFromOdometer')):
            selected.append(document)
    return selected


def _year(value):
    match = re.search(r'\b(20\d{2})\b', _nested_text(value))
    return int(match.group(1)) if match else None


def _canonical_url(url):
    parsed = urlsplit(url)
    return f'{parsed.scheme.lower()}://{parsed.netloc.lower()}{parsed.path.rstrip("/") or "/"}'


def _evidence(text, pattern):
    match = re.search(pattern, text, re.IGNORECASE)
    return match.group(0) if match else ''


def parse_listing_html(body, url, context=None):
    if not is_trusted_url(url):
        return None
    parser = _ListingParser()
    parser.feed(body)
    documents = _structured_documents(parser)
    vehicle_documents = _vehicle_documents(documents)
    page_text = html.unescape(' '.join(parser.visible))
    structured_text = ' '.join(_nested_text(document) for document in documents)
    evidence_text = f'{page_text} {structured_text}'
    vehicle_text = ' '.join(_nested_text(document) for document in vehicle_documents)
    name = _first(vehicle_documents, 'name', 'model', 'title') or parser.meta.get('og:title') or ''.join(parser.title)
    offers = _first(documents, 'offers') or {}
    mileage = _first(documents, 'mileageFromOdometer', 'mileage')
    address = _first(documents, 'address')
    image = _first(documents, 'image') or parser.meta.get('og:image')
    if isinstance(image, list):
        image = image[0] if image else None
    if isinstance(image, dict):
        image = image.get('url')
    if not isinstance(offers, dict):
        offers = {}
    if isinstance(mileage, dict):
        mileage = mileage.get('value')
    if isinstance(address, dict):
        location = address.get('addressLocality') or address.get('addressRegion') or address.get('postalCode')
    else:
        location = address
    context = context or {}
    name = _nested_text(name).strip()
    vehicle_evidence_text = f'{name} {vehicle_text}'
    haystack = vehicle_evidence_text.lower()
    if 'multivan' in haystack and re.search(r'\b(lang|long|l2)\b', haystack):
        profile = 'vw_multivan_long'
    elif 'tourneo custom' in haystack and re.search(r'\b(lang|long|l2)\b', haystack):
        profile = 'ford_tourneo_custom_long'
    else:
        profile = context.get('profile')
    long_pattern = r'\b(?:lang|long|l2|lwb)\b'
    if re.search(long_pattern, vehicle_evidence_text, re.IGNORECASE):
        long_evidence = _evidence(page_text, long_pattern) or _evidence(vehicle_evidence_text, long_pattern)
    else:
        long_evidence = ''
    accident_evidence = _evidence(evidence_text, r'\bunfallfrei\b')
    parsed_url = _canonical_url(parser.canonical or url)
    return {
        'profile': profile,
        'title': name,
        'price_eur': offers.get('price'),
        'mileage_km': mileage,
        'year': _year(_first(documents, 'vehicleModelDate', 'dateFirstRegistered', 'productionDate')),
        'location': _nested_text(location).strip(),
        'distance_km': context.get('distance_km'),
        'image_url': image,
        'url': parsed_url,
        'source': (urlsplit(url).hostname or '').lower(),
        'long_version_evidence': long_evidence,
        'accident_free_evidence': accident_evidence,
    }


def _request(url, headers=None):
    request = urllib.request.Request(url, headers=headers or {'User-Agent': 'Hermes-Auto-Screener/1.0'})
    return urllib.request.urlopen(request, timeout=20)


def _parse_search_response(raw, endpoint):
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        hostname = (urlsplit(endpoint).hostname or '').lower()
        if hostname.endswith('autoscout24.de'):
            urls = []
            for path in re.findall(r'/angebote/[^"\\<>\s]+', raw):
                path = html.unescape(path).replace('\\/', '/').rstrip('},')
                url = 'https://www.autoscout24.de' + path
                if url not in urls:
                    urls.append(url)
            payload = {'results': [{'url': url} for url in urls]}
        elif not (hostname.endswith('duckduckgo.com') or hostname.endswith('bing.com')):
            raise
        else:
            parser = _SearchResultParser()
            parser.feed(raw)
            payload = {'results': [{'url': url} for url in parser.urls]}
    if not isinstance(payload, dict) or not isinstance(payload.get('results'), list):
        raise ValueError('Suchantwort braucht eine results-Liste')
    return payload


@lru_cache(maxsize=256)
def _location_distance(location):
    normalized = str(location or '').strip()
    if not normalized:
        return None
    if 'norderstedt' in normalized.lower():
        return 0.0
    endpoint = os.environ.get('AUTO_MARKET_GEOCODER_URL', DEFAULT_GEOCODER_URL).strip()
    if not endpoint:
        return None
    query = urlencode({'q': f'{normalized}, Deutschland', 'format': 'jsonv2', 'limit': 1, 'countrycodes': 'de'})
    try:
        with _request(endpoint + ('&' if '?' in endpoint else '?') + query, headers={
            'User-Agent': 'Hermes-Auto-Screener/1.0 (Norderstedt vehicle search)',
            'Accept': 'application/json',
        }) as response:
            payload = json.loads(response.read(MAX_RESPONSE_BYTES).decode('utf-8'))
        item = payload[0] if isinstance(payload, list) and payload else None
        lat = float(item['lat']) if item else None
        lon = float(item['lon']) if item else None
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    origin_lat, origin_lon = math.radians(53.7064), math.radians(9.9933)
    target_lat, target_lon = math.radians(lat), math.radians(lon)
    delta_lat = target_lat - origin_lat
    delta_lon = target_lon - origin_lon
    haversine = math.sin(delta_lat / 2) ** 2 + math.cos(origin_lat) * math.cos(target_lat) * math.sin(delta_lon / 2) ** 2
    return round(6371.0 * 2 * math.asin(math.sqrt(haversine)), 1)


def fetch_listing(url):
    if not is_trusted_url(url):
        return {'status_code': 403, 'url': url, 'error': 'Quelle nicht freigegeben'}
    try:
        with _request(url) as response:
            body = response.read(MAX_RESPONSE_BYTES).decode('utf-8', errors='replace')
            return parse_listing_html(body, url) or {'status_code': response.status, 'url': url}
    except urllib.error.HTTPError as exc:
        return {'status_code': exc.code, 'url': url}
    except (OSError, ValueError, UnicodeError) as exc:
        return {'status_code': 503, 'url': url, 'error': type(exc).__name__}


def _search_query(profile):
    model = 'VW Multivan' if profile['id'] == 'vw_multivan_long' else 'Ford Tourneo Custom'
    return f'{model} lang Norderstedt 250 km Baujahr ab 2025 maximal 40000 km maximal 40000 EUR unfallfrei'


def _search_url(profile, configured_endpoint):
    if configured_endpoint:
        query = urlencode({'q': _search_query(profile), 'profile': profile['id'], 'location': 'Norderstedt', 'radius_km': 250, 'limit': 50})
        return configured_endpoint + ('&' if '?' in configured_endpoint else '?') + query
    endpoint = DEFAULT_SOURCE_SEARCH_URLS.get(profile['id'], DEFAULT_SEARCH_URL)
    if endpoint == DEFAULT_SEARCH_URL:
        query = urlencode({'q': _search_query(profile), 'profile': profile['id'], 'location': 'Norderstedt', 'radius_km': 250, 'limit': 50})
    else:
        query = urlencode({'atype': 'C', 'cy': 'D', 'kmto': 40000, 'priceto': 40000, 'sort': 'standard'})
    return endpoint + ('&' if '?' in endpoint else '?') + query


def discover_candidates():
    configured_endpoint = os.environ.get('AUTO_MARKET_SEARCH_URL', '').strip()
    token = os.environ.get('AUTO_MARKET_SEARCH_TOKEN', '').strip()
    candidates = []
    for profile in auto_market.SEARCH_PROFILES:
        endpoint = configured_endpoint or DEFAULT_SOURCE_SEARCH_URLS.get(profile['id'], DEFAULT_SEARCH_URL)
        search_url = _search_url(profile, configured_endpoint)
        headers = {'User-Agent': 'Hermes-Auto-Screener/1.0', 'Accept': 'application/json'}
        if token:
            headers['Authorization'] = f'Bearer {token}'
        try:
            with _request(search_url, headers=headers) as response:
                raw = response.read(MAX_RESPONSE_BYTES).decode('utf-8')
                payload = _parse_search_response(raw, endpoint)
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f'Suchantwort ungueltig: {type(exc).__name__}') from exc
        for result in payload['results']:
            if not isinstance(result, dict) or not is_trusted_url(result.get('url', '')):
                continue
            listing = fetch_listing(result['url'])
            if listing.get('status_code') in (403, 404, 410, 503) or not listing.get('title'):
                continue
            listing['profile'] = profile['id']
            listing['distance_km'] = result.get('distance_km', listing.get('distance_km'))
            if result.get('location'):
                listing['location'] = result['location']
            if listing.get('distance_km') is None:
                listing['distance_km'] = _location_distance(listing.get('location'))
            if listing.get('distance_km') is not None:
                candidates.append(listing)
    return candidates
