from django.core.management.utils import get_random_secret_key
from decouple import Csv, config
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

DEBUG = config(
    'DEBUG',
    default=False,
    cast=lambda value: str(value).strip().lower() in {'1', 'true', 'yes', 'on', 'debug', 'development'},
)
# Dev may run without a key; production must set one (a random per-process key
# silently breaks sessions and reset links across workers/restarts).
SECRET_KEY = config('DJANGO_SECRET_KEY', default=get_random_secret_key() if DEBUG else '')
if not SECRET_KEY:
    from django.core.exceptions import ImproperlyConfigured
    raise ImproperlyConfigured('Set DJANGO_SECRET_KEY in .env')
ALLOWED_HOSTS = config('ALLOWED_HOSTS', default='localhost,127.0.0.1', cast=Csv())

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django.contrib.sites',
    'allauth',
    'allauth.account',
    'allauth.socialaccount',
    'allauth.socialaccount.providers.google',
    'accounts',
    'shops',
    'orders',
    'payments',
    'maps',
    'reviews',
    'notifications',
    'analytics',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'allauth.account.middleware.AccountMiddleware',
]

ROOT_URLCONF = 'clefresh.urls'

TEMPLATES = [{
    'BACKEND': 'django.template.backends.django.DjangoTemplates',
    'DIRS': [BASE_DIR / 'templates'],
    'APP_DIRS': True,
    'OPTIONS': {
        'context_processors': [
            'django.template.context_processors.debug',
            'django.template.context_processors.request',
            'django.contrib.auth.context_processors.auth',
            'django.contrib.messages.context_processors.messages',
            'clefresh.context_processors.google_maps_key',
            'clefresh.context_processors.paymongo_public_key',
            'clefresh.context_processors.navigation_context',
        ],
        'libraries': {
            'shop_extras': 'shops.templatetags.shop_extras',
        },
    },
}]

WSGI_APPLICATION = 'clefresh.wsgi.application'

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Asia/Manila'
USE_I18N = True
USE_TZ = True

MEDIA_URL = '/media/'
MEDIA_ROOT = BASE_DIR / 'media'

STATIC_URL = '/static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATIC_ROOT = BASE_DIR / 'staticfiles'          # `collectstatic` target, served by nginx in production
# Production: STATIC_HASHED_FILENAMES=True gives every static file a content-hashed name
# (style.3f2a9c.css), so nginx can cache them for a year and a changed file is a new URL.
# Requires `python manage.py collectstatic` on each deploy (missing manifest = 500 errors).
if config('STATIC_HASHED_FILENAMES', default=False, cast=bool):
    STORAGES = {
        'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
        'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.ManifestStaticFilesStorage'},
    }

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

AUTH_USER_MODEL = 'accounts.User'
SITE_ID = 1

AUTHENTICATION_BACKENDS = [
    'django.contrib.auth.backends.ModelBackend',
    'allauth.account.auth_backends.AuthenticationBackend',
]

ACCOUNT_LOGIN_METHODS = {'email'}
ACCOUNT_SIGNUP_FIELDS = ['email*', 'password1*', 'password2*']
ACCOUNT_EMAIL_VERIFICATION = 'none'
ACCOUNT_ADAPTER = 'accounts.adapters.AccountAdapter'
SOCIALACCOUNT_ADAPTER = 'accounts.adapters.SocialAccountAdapter'
# New Google users are paused by SocialAccountAdapter for a CLEFRESH email
# code before the allauth social account is saved.
SOCIALACCOUNT_AUTO_SIGNUP = True
SOCIALACCOUNT_LOGIN_ON_GET = True
LOGIN_URL = '/auth/login/'
LOGIN_REDIRECT_URL = '/'
LOGOUT_REDIRECT_URL = '/'

SESSION_COOKIE_SECURE = config('SESSION_COOKIE_SECURE', default=False, cast=bool)
SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_SECURE = config('CSRF_COOKIE_SECURE', default=False, cast=bool)
SECURE_SSL_REDIRECT = config('SECURE_SSL_REDIRECT', default=False, cast=bool)
SECURE_HSTS_SECONDS = config('SECURE_HSTS_SECONDS', default=0, cast=int)
SECURE_HSTS_INCLUDE_SUBDOMAINS = config('SECURE_HSTS_INCLUDE_SUBDOMAINS', default=False, cast=bool)
SECURE_HSTS_PRELOAD = config('SECURE_HSTS_PRELOAD', default=False, cast=bool)
CSRF_TRUSTED_ORIGINS = config('CSRF_TRUSTED_ORIGINS', default='', cast=Csv())
X_FRAME_OPTIONS = 'DENY'
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = 'strict-origin-when-cross-origin'
DATA_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024

SITE_URL = config('SITE_URL', default='http://localhost:8000')

# Behind nginx, set CLIENT_IP_HEADER=HTTP_X_REAL_IP (and `proxy_set_header X-Real-IP $remote_addr;`)
# so rate limits see each visitor, not the proxy. Leave empty when Django faces the internet directly.
CLIENT_IP_HEADER = config('CLIENT_IP_HEADER', default='')

# Login limits, verification codes and rate counters live in the cache, so every server
# process must share it. Dev: in-memory. Production: CACHE_BACKEND=redis (REDIS_URL) or
# CACHE_BACKEND=db (then run `python manage.py createcachetable` once).
_CACHE_BACKEND = config('CACHE_BACKEND', default='locmem')
if _CACHE_BACKEND == 'redis':
    CACHES = {'default': {'BACKEND': 'django.core.cache.backends.redis.RedisCache',
                          'LOCATION': config('REDIS_URL', default='redis://127.0.0.1:6379/1')}}
elif _CACHE_BACKEND == 'db':
    CACHES = {'default': {'BACKEND': 'django.core.cache.backends.db.DatabaseCache',
                          'LOCATION': 'clefresh_cache'}}
else:
    CACHES = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}

# Online payment follow-up (orders that must be paid online before delivery):
# reminders N hours after the price is final, then auto-switch to cash on delivery at the deadline.
PAYMENT_REMINDER_HOURS = [int(h) for h in config('PAYMENT_REMINDER_HOURS', default='24,48', cast=Csv())]
PAYMENT_DUE_HOURS = config('PAYMENT_DUE_HOURS', default=72, cast=int)

# API Keys
PAYMONGO_SECRET_KEY = config('PAYMONGO_SECRET_KEY', default='')
PAYMONGO_PUBLIC_KEY = config('PAYMONGO_PUBLIC_KEY', default='')
PAYMONGO_WEBHOOK_SECRET = config('PAYMONGO_WEBHOOK_SECRET', default='')
GOOGLE_MAPS_API_KEY = config('GOOGLE_MAPS_API_KEY', default='')
FORMSPREE_ENDPOINT = config('FORMSPREE_ENDPOINT', default='')

EMAIL_BACKEND = config(
    'EMAIL_BACKEND',
    default='django.core.mail.backends.smtp.EmailBackend',
)
EMAIL_HOST = config('EMAIL_HOST', default='')
EMAIL_PORT = config('EMAIL_PORT', default=587, cast=int)
EMAIL_USE_TLS = config('EMAIL_USE_TLS', default=True, cast=bool)
EMAIL_HOST_USER = config('EMAIL_HOST_USER', default='')
EMAIL_HOST_PASSWORD = config('EMAIL_HOST_PASSWORD', default='')
DEFAULT_FROM_EMAIL = config('DEFAULT_FROM_EMAIL', default=EMAIL_HOST_USER or 'noreply@clefresh.local')

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '{levelname} {asctime} {module} {process:d} {message}',
            'style': '{',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'verbose',
        },
        'file': {
            'class': 'logging.handlers.RotatingFileHandler',
            'filename': BASE_DIR / 'logs' / 'clefresh.log',
            'maxBytes': 1024 * 1024 * 5,
            'backupCount': 5,
            'formatter': 'verbose',
        },
    },
    'root': {
        'handlers': ['console', 'file'],
        'level': 'WARNING',
    },
    'loggers': {
        'django': {
            'handlers': ['console', 'file'],
            'level': 'WARNING',
            'propagate': False,
        },
    },
}

GOOGLE_CLIENT_ID = config('GOOGLE_CLIENT_ID', default='')
GOOGLE_CLIENT_SECRET = config('GOOGLE_CLIENT_SECRET', default='')

SOCIALACCOUNT_PROVIDERS = {
    'google': {
        'SCOPE': ['profile', 'email'],
        'AUTH_PARAMS': {
            'access_type': 'online',
            # Always show Google's account chooser instead of silently using
            # an existing Google browser session from the registration button.
            'prompt': 'select_account',
        },
        # Credentials live in the DB SocialApp row (synced from .env by
        # accounts.apps._sync_google_social_app on each migrate).
    },
}
