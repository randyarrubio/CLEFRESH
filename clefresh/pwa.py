"""Progressive Web App: web manifest, root-scoped service worker, and the offline fallback page.

Served by Django (not as plain static files) so the service worker can live at /sw.js with scope
'/', and both files can point at hashed static URLs when STATIC_HASHED_FILENAMES is on.
"""
import json

from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.templatetags.static import static
from django.views.decorators.cache import cache_control
from django.views.decorators.http import require_GET

# Bump when the precache list or caching rules change, so old caches are dropped on activate.
SW_VERSION = 'clefresh-v2'


@require_GET
@cache_control(max_age=3600, public=True)
def manifest_view(request):
    data = {
        'name': 'CLEFRESH — Laundry Pickup & Delivery',
        'short_name': 'CLEFRESH',
        'description': 'Book laundry pickup and delivery from shops near you in Cantilan.',
        'id': '/',
        'start_url': '/?source=pwa',
        'scope': '/',
        'display': 'standalone',
        'orientation': 'portrait',
        'background_color': '#ffffff',
        'theme_color': '#1287d9',
        'lang': 'en-PH',
        'categories': ['lifestyle', 'shopping', 'utilities'],
        'icons': [
            {'src': static('img/icon-192.png'), 'sizes': '192x192', 'type': 'image/png', 'purpose': 'any'},
            {'src': static('img/icon-512.png'), 'sizes': '512x512', 'type': 'image/png', 'purpose': 'any'},
            {'src': static('img/icon-maskable-192.png'), 'sizes': '192x192', 'type': 'image/png', 'purpose': 'maskable'},
            {'src': static('img/icon-maskable-512.png'), 'sizes': '512x512', 'type': 'image/png', 'purpose': 'maskable'},
        ],
        'shortcuts': [
            {'name': 'My Orders', 'url': '/orders/', 'icons': [{'src': static('img/icon-192.png'), 'sizes': '192x192'}]},
        ],
    }
    return JsonResponse(data, content_type='application/manifest+json')


@require_GET
@cache_control(no_cache=True, max_age=0)       # browsers must re-check the worker on every visit
def service_worker_view(request):
    precache = [
        '/offline/',
        static('css/style.css'),
        static('js/main.js'),
        static('img/logo.webp'),
        static('img/icon-192.png'),
        static('img/favicon-32.png'),
        static('vendor/bootstrap-icons-1.11.3/bootstrap-icons.min.css'),
    ]
    from decouple import config
    admin_prefix = '/' + config('DJANGO_ADMIN_URL', default='django-admin/').strip('/') + '/'
    bypass = ['/payment/', '/auth/', '/api/', '/admin-panel/', admin_prefix, '/media/', '/sw.js']
    response = render(request, 'pwa/sw.js', {'version': SW_VERSION, 'precache_json': json.dumps(precache),
                                              'bypass_json': json.dumps(bypass)},
                      content_type='application/javascript')
    response['Service-Worker-Allowed'] = '/'
    return response


@require_GET
@cache_control(max_age=86400, public=True)
def offline_view(request):
    return render(request, 'pwa/offline.html')
