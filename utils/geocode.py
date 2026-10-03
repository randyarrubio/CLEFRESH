import logging
import requests
from django.conf import settings

logger = logging.getLogger(__name__)


def geocode_address(address):
    if not settings.GOOGLE_MAPS_API_KEY:
        return None, None
    try:
        resp = requests.get(
            'https://maps.googleapis.com/maps/api/geocode/json',
            params={'address': address, 'key': settings.GOOGLE_MAPS_API_KEY},
            timeout=5
        ).json()
        if resp.get('status') == 'OK':
            loc = resp['results'][0]['geometry']['location']
            return loc['lat'], loc['lng']
    except Exception:
        logger.exception("Geocoding failed for address: %s", address)
    return None, None
