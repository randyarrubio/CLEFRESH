# CLEFRESH

CLEFRESH is a Django laundry shop booking platform. Customers can find shops, book pickup and delivery, and pay online. Shop owners manage services and orders, while riders handle deliveries.

## Requirements

- Python 3.10 or newer
- pip

The application uses SQLite by default for local development. Payment processing, Google Maps, Google sign-in, and email require credentials from their respective providers; the app can be explored locally without configuring those integrations.

## Local setup

From the project root, create and activate a virtual environment, install dependencies, and create a local environment file:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Set a private `DJANGO_SECRET_KEY` in `.env` before deploying. For local development, the example file enables debug mode. Do not commit `.env` or put real credentials in `.env.example`.

Initialize the database and run the development server:

```powershell
python manage.py migrate
python manage.py runserver
```

Open <http://127.0.0.1:8000/>. Create an administrator with `python manage.py createsuperuser` if you need the Django admin.

## Integrations

Set provider credentials in `.env` to enable PayMongo payments, Google Maps, Google OAuth, Formspree, and SMTP email. See `.env.example` for supported variable names. For production, set `DEBUG=False`, provide `DJANGO_SECRET_KEY` and `ALLOWED_HOSTS`, use HTTPS, and configure secure cookies, trusted CSRF origins, email, a shared cache backend, and static/media serving for your deployment. The example nginx configuration is in `deploy/nginx.conf.example`.

For deployments that enable `STATIC_HASHED_FILENAMES`, run `python manage.py collectstatic` as part of each release.

## Data and uploads

SQLite databases, database backups, logs, local settings, and uploaded media are excluded from Git. Back up production data and user uploads using your deployment's storage and backup process; they are not part of a source checkout.

## Tests

Run the Django test suite with:

```powershell
python manage.py test
```
