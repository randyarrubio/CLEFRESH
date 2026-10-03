import secrets
import time
from types import SimpleNamespace

from django.core.cache import cache

from utils.email_notifications import send_email_notification


PENDING_SSO_SESSION_KEY = 'pending_google_sso'
PENDING_SSO_TOKEN_SESSION_KEY = 'pending_google_sso_token'
PENDING_SSO_EMAIL_SESSION_KEY = 'pending_google_sso_email'
PENDING_SSO_CODE_TTL = 600
PENDING_SSO_RESEND_COOLDOWN = 60
PENDING_SSO_MAX_ATTEMPTS = 5


def pending_sso_code_key(token):
    return f'pending_sso_verify_code_{token}'


def pending_sso_attempts_key(token):
    return f'pending_sso_verify_attempts_{token}'


def pending_sso_resend_key(token):
    return f'pending_sso_verify_resend_{token}'


def start_pending_sso_verification(request, email, full_name=''):
    token = secrets.token_urlsafe(24)
    request.session[PENDING_SSO_TOKEN_SESSION_KEY] = token
    request.session[PENDING_SSO_EMAIL_SESSION_KEY] = email
    send_pending_sso_verification_code(request, email, full_name)
    return token


def send_pending_sso_verification_code(request, email, full_name=''):
    token = request.session.get(PENDING_SSO_TOKEN_SESSION_KEY)
    if not token:
        return None
    code = f'{secrets.randbelow(1_000_000):06d}'
    cache.set(pending_sso_code_key(token), code, PENDING_SSO_CODE_TTL)
    cache.delete(pending_sso_attempts_key(token))
    cache.set(
        pending_sso_resend_key(token),
        time.time() + PENDING_SSO_RESEND_COOLDOWN,
        PENDING_SSO_RESEND_COOLDOWN,
    )
    user = SimpleNamespace(
        email=email,
        email_notifications_enabled=True,
        get_full_name=lambda: full_name or email,
    )
    send_email_notification('email_verification', user, extra={'code': code}, force=True)
    return code


def pending_sso_resend_cooldown_remaining(request):
    token = request.session.get(PENDING_SSO_TOKEN_SESSION_KEY)
    if not token:
        return 0
    next_allowed = cache.get(pending_sso_resend_key(token), 0)
    return max(int(next_allowed - time.time()), 0)


def clear_pending_sso_verification(request):
    token = request.session.get(PENDING_SSO_TOKEN_SESSION_KEY)
    if token:
        cache.delete(pending_sso_code_key(token))
        cache.delete(pending_sso_attempts_key(token))
        cache.delete(pending_sso_resend_key(token))
    request.session.pop(PENDING_SSO_SESSION_KEY, None)
    request.session.pop(PENDING_SSO_TOKEN_SESSION_KEY, None)
    request.session.pop(PENDING_SSO_EMAIL_SESSION_KEY, None)
