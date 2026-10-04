from django.conf import settings


_MAP_PATHS = ('/orders/', '/rider/', '/shops/', '/shop-dashboard/', '/auth/rider/')

def google_maps_key(request):
    if request.path == '/' or any(request.path.startswith(p) for p in _MAP_PATHS):
        return {'GOOGLE_MAPS_API_KEY': settings.GOOGLE_MAPS_API_KEY}
    return {}


def paymongo_public_key(request):
    if request.path.startswith('/payment/'):
        return {'PAYMONGO_PUBLIC_KEY': settings.PAYMONGO_PUBLIC_KEY}
    return {}


def navigation_context(request):
    return {
        'is_shop_owner_area': request.path.startswith('/shop-dashboard/'),
        'NOTIFICATIONS_SSE': settings.NOTIFICATIONS_SSE,
    }
