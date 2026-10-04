# Deploying CLEFRESH to PythonAnywhere

Replace `USERNAME` with your PythonAnywhere username everywhere below.
Site URL: `https://USERNAME.pythonanywhere.com`

## 1. Get the code (Bash console)

```bash
git clone https://github.com/randyarrubio/CLEFRESH.git ~/CLEFRESH
cd ~/CLEFRESH
mkvirtualenv clefresh --python=python3.13
pip install -r requirements.txt
```

## 2. Create `~/CLEFRESH/.env`

`.env` is not in git — create it on the server (`nano ~/CLEFRESH/.env`):

```ini
DJANGO_SECRET_KEY=<run: python -c "import secrets; print(secrets.token_urlsafe(50))">
DEBUG=False
ALLOWED_HOSTS=USERNAME.pythonanywhere.com
CSRF_TRUSTED_ORIGINS=https://USERNAME.pythonanywhere.com
SITE_URL=https://USERNAME.pythonanywhere.com
DJANGO_ADMIN_URL=<something-non-obvious>/

# PythonAnywhere terminates HTTPS and forwards the visitor IP
TRUST_X_FORWARDED_PROTO=True
CLIENT_IP_HEADER=HTTP_X_REAL_IP
SESSION_COOKIE_SECURE=True
CSRF_COOKIE_SECURE=True
# Leave False — use the Web tab's "Force HTTPS" toggle instead
SECURE_SSL_REDIRECT=False

# Shared cache across uWSGI workers (rate limits, verification codes)
CACHE_BACKEND=db
# SSE would hold a worker per open tab; the bell polls instead
NOTIFICATIONS_SSE=False
STATIC_HASHED_FILENAMES=True

# Gmail SMTP (App Password, no spaces)
EMAIL_HOST=smtp.gmail.com
EMAIL_PORT=587
EMAIL_USE_TLS=True
EMAIL_HOST_USER=clefresh26@gmail.com
EMAIL_HOST_PASSWORD=
DEFAULT_FROM_EMAIL=CLEFRESH <clefresh26@gmail.com>

PAYMONGO_SECRET_KEY=
PAYMONGO_PUBLIC_KEY=
PAYMONGO_WEBHOOK_SECRET=
GOOGLE_MAPS_API_KEY=
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=
FORMSPREE_ENDPOINT=
```

Leave `DATABASE_URL` empty to use SQLite at `~/CLEFRESH/db.sqlite3`.

## 3. Database, cache table, static files

```bash
cd ~/CLEFRESH
python manage.py migrate
python manage.py createcachetable
python manage.py collectstatic --noinput
python manage.py createsuperuser
python manage.py check --deploy
```

## 4. Web tab

1. **Add a new web app** → **Manual configuration** → **Python 3.13**.
2. **Virtualenv:** `/home/USERNAME/.virtualenvs/clefresh`
3. **Source code / Working directory:** `/home/USERNAME/CLEFRESH`
4. **WSGI configuration file:** replace its contents with `deploy/pythonanywhere_wsgi.py` (set `USERNAME`).
5. **Static files** — add these mappings only. Do **not** map all of `/media/`;
   `rider_docs/` and `shop_docs/` are private and must stay unmapped so they go through Django's auth views.

   | URL | Directory |
   |-----|-----------|
   | `/static/` | `/home/USERNAME/CLEFRESH/staticfiles` |
   | `/media/profiles/` | `/home/USERNAME/CLEFRESH/media/profiles` |
   | `/media/shop_logos/` | `/home/USERNAME/CLEFRESH/media/shop_logos` |
   | `/media/shop_banners/` | `/home/USERNAME/CLEFRESH/media/shop_banners` |
   | `/media/proof_photos/` | `/home/USERNAME/CLEFRESH/media/proof_photos` |

6. **Security:** turn on **Force HTTPS**.
7. Click **Reload**.

## 5. External services

- **Google OAuth** (Cloud Console → Credentials): add redirect URI
  `https://USERNAME.pythonanywhere.com/accounts/google/login/callback/` and JS origin `https://USERNAME.pythonanywhere.com`.
  Re-run `python manage.py migrate` after changing `GOOGLE_CLIENT_*` (it syncs the SocialApp row).
- **Google Maps key**: add `https://USERNAME.pythonanywhere.com/*` to its HTTP referrer restrictions.
- **PayMongo webhook**: `https://USERNAME.pythonanywhere.com/payment/webhook/` — copy its signing secret into `PAYMONGO_WEBHOOK_SECRET`.

## 6. Scheduled task (Tasks tab)

```
/home/USERNAME/.virtualenvs/clefresh/bin/python /home/USERNAME/CLEFRESH/manage.py process_payment_reminders
```

Hourly on paid plans; free accounts get one daily task (reminders/COD switch then lag up to 24 h).

## Free-account limits

- Outbound HTTP goes through a proxy that only allows [whitelisted sites](https://www.pythonanywhere.com/whitelist/).
  Google APIs are on it; check `api.paymongo.com` and `formspree.io` — if missing, payments fail until you
  request whitelisting or upgrade.
- Gmail SMTP works on free accounts.
- SQLite is fine at this scale; MySQL/Postgres are paid add-ons.

## Updating

```bash
cd ~/CLEFRESH && git pull
workon clefresh
pip install -r requirements.txt
python manage.py migrate
python manage.py collectstatic --noinput
```
Then **Reload** on the Web tab.

Uploading existing media: zip `media/` locally, upload via the Files tab, `unzip` into `~/CLEFRESH/`.
