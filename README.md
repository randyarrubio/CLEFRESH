# CLEFRESH

CLEFRESH is a Django laundry shop booking platform. Customers can find shops, book pickup and delivery, and pay online. Shop owners manage services and orders, while riders handle deliveries.

## Requirements

- Python 3.13 (the project is tested and deployed with this version)
- pip

The application uses the tracked SQLite database by default. PostgreSQL is supported for production via `DATABASE_URL`. Payment processing, Google Maps, Google sign-in, and email require credentials from their respective providers; the app can be explored locally without configuring those integrations.

## Local setup

From the project root, create and activate a virtual environment, install dependencies, and create a local environment file:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Set a private `DJANGO_SECRET_KEY` in `.env` before deploying. Do not commit `.env` or put real credentials in `.env.example`.

Initialize the database and run the development server:

```powershell
python manage.py migrate
python manage.py runserver
```

Open <http://127.0.0.1:8000/>. Create an administrator with `python manage.py createsuperuser` if you need the Django admin.

## Integrations

Set provider credentials in `.env` to enable PayMongo payments, Google Maps, and Google OAuth. SMTP is required for password resets and verification codes. Configure `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_USE_TLS` or `EMAIL_USE_SSL`, optional `EMAIL_HOST_USER` and `EMAIL_HOST_PASSWORD`, and `DEFAULT_FROM_EMAIL`; Formspree is only a contact-form relay and is not used for transactional messages.

For production, set `DEBUG=False`, provide `DJANGO_SECRET_KEY` and `ALLOWED_HOSTS`, use HTTPS, and configure secure cookies, trusted CSRF origins, SMTP, a shared cache backend, and static/media serving. Set `TRUST_X_FORWARDED_PROTO=True` only when a trusted TLS reverse proxy sets `X-Forwarded-Proto`. The nginx example assumes PostgreSQL and Gunicorn. Set `DATABASE_URL` to a PostgreSQL URL and install the pinned production dependencies from `requirements.txt`. SQLite remains the default for local development. For a local-only SQLite database at another path, set `DATABASE_URL` to a SQLite URL such as `sqlite:////absolute/path/to/db.sqlite3`.

For deployments that enable `STATIC_HASHED_FILENAMES`, run `python manage.py collectstatic` as part of each release.

## Data, uploads, and GitHub

The current `db.sqlite3` is tracked and will be included in Git commits. Inspect it for personal or customer data before pushing to a public repository. Dated SQLite backups, `.env`, logs, machine-local settings, and uploaded media are excluded. Media files are stored outside Git, so database rows may refer to files absent from a clone. Keep production database and media backups in private storage. Removing a database from a future commit does not erase it from earlier Git history.

## Tests

Run the Django test suite with:

```powershell
python manage.py test
```
