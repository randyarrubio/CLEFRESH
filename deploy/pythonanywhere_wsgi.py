# PythonAnywhere WSGI file.
# Paste this into the file linked from the Web tab ("WSGI configuration file"),
# replacing everything in it. Change USERNAME to your PythonAnywhere username.
import os
import sys

PROJECT_DIR = '/home/USERNAME/CLEFRESH'

if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

# python-decouple finds PROJECT_DIR/.env from settings.py's location; chdir keeps
# relative paths (logs/, media/) predictable too.
os.chdir(PROJECT_DIR)
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'clefresh.settings')

from django.core.wsgi import get_wsgi_application  # noqa: E402

application = get_wsgi_application()
