from django.contrib import admin
from django.urls import path, re_path, include
from django.views.generic import RedirectView
from django.conf import settings
from django.conf.urls.static import static
from decouple import config
from accounts.views import sso_complete_view

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
    path('', include('shops.urls')),
] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
