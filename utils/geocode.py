"""Address -> (lat, lng).

Google's Geocoding API is used when GOOGLE_MAPS_API_KEY is set and works; otherwise (no key, or
the API isn't enabled) OpenStreetMap's free Nominatim service is used. Results are cached, and
Nominatim's usage policy is respected: at most one request per second and an identifying
User-Agent. Local addresses like "Purok 3, Brgy. Lininti-an, Cantilan" often aren't mapped down
to the purok, so Nominatim falls back to broader parts of the address (street -> barangay -> town).

Tests never touch the network (settings.TESTING).
"""
import hashlib
import logging
import re
import threading
import time

import requests
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

CACHE_HIT_SECONDS = 60 * 60 * 24 * 30      # an address rarely moves
CACHE_MISS_SECONDS = 60 * 60 * 6           # retry unknown addresses later
NOMINATIM_URL = 'https://nominatim.openstreetmap.org/search'
_nominatim_lock = threading.Lock()
_nominatim_last = [0.0]
_google_disabled_until = [0.0]             # skip Google for a while after REQUEST_DENIED


def _user_agent():
    site = getattr(settings, 'SITE_URL', '') or 'local'
    return f'CLEFRESH/1.0 laundry booking platform (+{site})'


def _google(address):
    key = getattr(settings, 'GOOGLE_MAPS_API_KEY', '')
    if not key or time.time() < _google_disabled_until[0]:
        return None, None
    try:
        resp = requests.get('https://maps.googleapis.com/maps/api/geocode/json',
                            params={'address': address, 'key': key}, timeout=5).json()
    except Exception:
        logger.exception('Google geocoding failed for %s', address)
        return None, None
    status = resp.get('status')
    if status == 'OK':
        loc = resp['results'][0]['geometry']['location']
        return loc['lat'], loc['lng']
    if status in ('REQUEST_DENIED', 'OVER_DAILY_LIMIT'):
        # Key missing the Geocoding API, billing off, etc.: use OpenStreetMap for the next hour.
        logger.warning('Google geocoding unavailable (%s); using OpenStreetMap', status)
        _google_disabled_until[0] = time.time() + 3600
    return None, None


def _nominatim_query(q):
    with _nominatim_lock:                  # one request per second, per process
        wait = 1.05 - (time.time() - _nominatim_last[0])
        if wait > 0:
            time.sleep(wait)
        try:
            resp = requests.get(NOMINATIM_URL, timeout=8, headers={'User-Agent': _user_agent()},
                                params={'q': q, 'format': 'jsonv2', 'limit': 1, 'countrycodes': 'ph'})
            data = resp.json() if resp.ok else []
        except Exception:
            logger.exception('Nominatim geocoding failed for %s', q)
            data = []
        finally:
            _nominatim_last[0] = time.time()
    if data:
        return float(data[0]['lat']), float(data[0]['lon'])
    return None, None


def _nominatim(address):
    """Try the full address, then drop leading parts (purok, street) until something matches."""
    # "Brgy." confuses OpenStreetMap and puroks/sitios aren't mapped, so normalise before searching.
    parts = []
    for part in address.replace('\n', ',').split(','):
        part = re.sub(r'\b(brgy\.?|bgy\.?|barangay)\s*', '', part.strip(), flags=re.IGNORECASE).strip()
        if part and not re.match(r'^(purok|prk\.?|sitio)\b', part, re.IGNORECASE):
            parts.append(part)
    for start in range(0, max(len(parts) - 1, 1)):
        query = ', '.join(parts[start:])
        if 'philippines' not in query.lower():
            query += ', Philippines'
        lat, lng = _nominatim_query(query)
        if lat is not None:
            return lat, lng
    return None, None


def geocode_address(address):
    address = (address or '').strip()
    if not address or getattr(settings, 'TESTING', False):
        return None, None
    key = 'geocode:' + hashlib.sha1(address.lower().encode('utf-8')).hexdigest()
    cached = cache.get(key)
    if cached is not None:
        return tuple(cached)
    lat, lng = _google(address)
    if lat is None:
        lat, lng = _nominatim(address)
    cache.set(key, (lat, lng), CACHE_HIT_SECONDS if lat is not None else CACHE_MISS_SECONDS)
    return lat, lng


def geocode_order_in_background(order_id):
    """Fill an order's pickup/delivery coordinates without making the customer wait."""
    if getattr(settings, 'TESTING', False):
        return

    def run():
        from django.db import connection
        from orders.models import Order
        try:
            order = Order.objects.filter(pk=order_id).only('pickup_address', 'delivery_address').first()
            if not order:
                return
            p_lat, p_lng = geocode_address(order.pickup_address)
            d_lat, d_lng = (p_lat, p_lng) if order.delivery_address == order.pickup_address \
                else geocode_address(order.delivery_address)
            Order.objects.filter(pk=order_id).update(pickup_lat=p_lat, pickup_lng=p_lng,
                                                     delivery_lat=d_lat, delivery_lng=d_lng)
        except Exception:
            logger.exception('Background geocoding failed for order %s', order_id)
        finally:
            connection.close()

    threading.Thread(target=run, daemon=True).start()
