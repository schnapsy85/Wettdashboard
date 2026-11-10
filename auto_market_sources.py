import html
import json
import os
import re
import urllib.error
import urllib.request
from html.parser import HTMLParser
from urllib.parse import urlencode, urlsplit

import auto_market


DEFAULT_TRUSTED_DOMAINS = ('mobile.de', 'autoscout24.de', 'hey.car', 'autouncle.de', 'autohero.com', 'kleinanzeigen.de')
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
    page_text = html.unescape(' '.join(parser.visible))
    structured_text = ' '.join(_nested_text(document) for document in documents)
    evidence_text = f'{page_text} {structured_text}'
    name = _first(documents, 'name', 'model', 'title') or parser.meta.get('og:title') or ''.join(parser.title)
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
    haystack = f'{name} {evidence_text}'.lower()
    if 'multivan' in haystack and re.search(r'\b(lang|long|l2)\b', haystack):
        profile = 'vw_multivan_long'
    elif 'tourneo custom' in haystack and re.search(r'\b(lang|long|l2)\b', haystack):
        profile = 'ford_tourneo_custom_long'
    else:
        profile = context.get('profile')
    long_evidence = _evidence(evidence_text, r'\b(?:lang|long|l2)\b')
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


def discover_candidates():
    endpoint = os.environ.get('AUTO_MARKET_SEARCH_URL', '').strip()
    if not endpoint:
        raise RuntimeError('AUTO_MARKET_SEARCH_URL nicht konfiguriert')
    token = os.environ.get('AUTO_MARKET_SEARCH_TOKEN', '').strip()
    candidates = []
    for profile in auto_market.SEARCH_PROFILES:
        query = urlencode({'q': _search_query(profile), 'profile': profile['id'], 'location': 'Norderstedt', 'radius_km': 250, 'limit': 50})
        headers = {'User-Agent': 'Hermes-Auto-Screener/1.0', 'Accept': 'application/json'}
        if token:
            headers['Authorization'] = f'Bearer {token}'
        try:
            with _request(endpoint + ('&' if '?' in endpoint else '?') + query, headers=headers) as response:
                payload = json.loads(response.read(MAX_RESPONSE_BYTES).decode('utf-8'))
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f'Suchantwort ungueltig: {type(exc).__name__}') from exc
        if not isinstance(payload, dict) or not isinstance(payload.get('results'), list):
            raise ValueError('Suchantwort braucht eine results-Liste')
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
            if listing.get('distance_km') is not None:
                candidates.append(listing)
    return candidates
