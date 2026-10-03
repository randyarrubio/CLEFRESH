import sys
from urllib.parse import urlparse

from django.apps import AppConfig
from django.db.models.signals import post_migrate


def _sync_site_from_settings():
    """Update the Site row's domain/name from SITE_URL so it isn't 'example.com'."""
    from django.conf import settings
    from django.contrib.sites.models import Site

    site_url = getattr(settings, 'SITE_URL', '') or 'http://localhost:8000'
    netloc = urlparse(site_url).netloc or 'localhost:8000'
    try:
        site = Site.objects.get(pk=settings.SITE_ID)
    except Exception:
        return None
    if site.domain != netloc or site.name != netloc:
        site.domain = netloc
        site.name = netloc
        site.save(update_fields=['domain', 'name'])
    return site


def _sync_google_social_app(sender, **kwargs):
    """Upsert the Google SocialApp row from .env (GOOGLE_CLIENT_ID/SECRET).

    Single source of truth: .env. No need to touch Django admin after editing.
    Also de-duplicates any extra google SocialApp rows that crept in.
    """
    from django.conf import settings

    site = _sync_site_from_settings()

    client_id = getattr(settings, 'GOOGLE_CLIENT_ID', '')
    secret = getattr(settings, 'GOOGLE_CLIENT_SECRET', '')

    if not client_id or not secret or site is None:
        return

    try:
        from allauth.socialaccount.models import SocialApp
    except Exception:
        return

    qs = SocialApp.objects.filter(provider='google').order_by('id')
    if qs.count() > 1:
        # Keep the oldest, delete the rest — prevents MultipleObjectsReturned
        # from allauth.socialaccount.adapter.get_app.
        SocialApp.objects.filter(provider='google').exclude(pk=qs.first().pk).delete()

    app, _ = SocialApp.objects.update_or_create(
        provider='google',
        defaults={
            'name': 'Google',
            'client_id': client_id,
            'secret': secret,
        },
    )
    if not app.sites.filter(pk=site.pk).exists():
        app.sites.add(site)


def _warn_if_placeholder_credentials():
    from django.conf import settings
    client_id = getattr(settings, 'GOOGLE_CLIENT_ID', '')
    if not client_id or 'xxxx' in client_id.lower():
        sys.stderr.write(
            '\n[CLEFRESH] WARNING: GOOGLE_CLIENT_ID in .env is missing or still a '
            'placeholder. Google SSO will return "Error 401: invalid_client" until '
            'real credentials are set. See README / CLAUDE.md.\n\n'
        )


class AccountsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'accounts'

    def ready(self):
        post_migrate.connect(_sync_google_social_app, sender=self)
        _warn_if_placeholder_credentials()
