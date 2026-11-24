from django.contrib import admin
from django.urls import path, re_path, include
from django.http import Http404
from django.views.generic import RedirectView
from django.conf import settings
from django.conf.urls.static import static
from decouple import config
from accounts.views import sso_complete_view
from . import pwa

def _private_media_404(request, *args):
    raise Http404


_ADMIN_URL = config('DJANGO_ADMIN_URL', default='django-admin/')

urlpatterns = [
    path(_ADMIN_URL, admin.site.urls),
    path('accounts/social/signup/', sso_complete_view, name='sso_complete'),
    # allauth is only used for Google SSO. Its email-management, password and local
    # sign-up/login pages would bypass CLEFRESH's own checks, so they redirect.
    re_path(r'^accounts/email/', RedirectView.as_view(pattern_name='profile')),
    re_path(r'^accounts/password/', RedirectView.as_view(pattern_name='forgot_password')),
    re_path(r'^accounts/signup/$', RedirectView.as_view(pattern_name='register')),
    re_path(r'^accounts/login/$', RedirectView.as_view(pattern_name='login', query_string=True)),
    path('accounts/', include('allauth.urls')),
    path('auth/', include('accounts.urls')),
    path('orders/', include('orders.urls')),
    path('orders/', include('reviews.urls')),
    path('payment/', include('payments.urls')),
    path('rider/', include('maps.urls')),
    path('shop-dashboard/', include('shops.dashboard_urls')),
    path('admin-panel/', include('analytics.urls')),
    path('api/', include('notifications.urls')),
    path('manifest.webmanifest', pwa.manifest_view, name='pwa_manifest'),
    path('sw.js', pwa.service_worker_view, name='pwa_service_worker'),
    path('offline/', pwa.offline_view, name='pwa_offline'),
    path('', include('shops.urls')),
    # Private uploads are only reachable through the permission-checked views in accounts.
    re_path(r'^media/(rider_docs|face_selfies|id_documents|shop_docs)/', _private_media_404),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

if not settings.DEBUG and settings.SERVE_MEDIA:
    # No nginx in front (e.g. Railway): Django serves the public uploads (shop photos, avatars,
    # proof photos) from the media volume. Private folders are 404'd by the rule above first.
    from django.views.decorators.cache import cache_control
    from django.views.static import serve as _serve

    @cache_control(max_age=86400, public=True)
    def _serve_public_media(request, path):
        return _serve(request, path, document_root=settings.MEDIA_ROOT)

    urlpatterns += [re_path(r'^media/(?P<path>.*)$', _serve_public_media)]
