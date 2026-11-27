"""Create the five real Cantilan laundry shops with demo owner logins.

    python manage.py seed_cantilan_shops                 # create / update shops, print where logins went
    python manage.py seed_cantilan_shops --reset-passwords

Shop names, addresses and photos come from the owner survey in `shop data/`. Lava'z Laundry Shop
uses its real price list (₱30 / ₱45 / ₱65 per kg, charged per load: ₱150 per 5 kg load of clothes,
₱180 per 4 kg load of bedsheets/towels/curtains, ₱260 per 4 kg load of comforters) and 8:00 AM – 6:00 PM; the other four shops use
PLACEHOLDER prices and hours near Lava'z (replace them with each shop's survey answers). Map pins
are OpenStreetMap geocodes of each address (street-level where the street is mapped, otherwise
the barangay centre). Owners get demo emails on the reserved `.test` domain, so no real inbox is
ever written to; new passwords are written to `shop data/demo_logins.txt` (git-ignored), never
printed. Re-running updates the shops in place and keeps existing passwords.
"""
import secrets
from datetime import time
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files import File
from django.core.management.base import BaseCommand
from django.db import transaction

from shops.models import Service, Shop

DATA_DIR = Path(settings.BASE_DIR) / 'shop data'
LOGINS_FILE = DATA_DIR / 'demo_logins.txt'

SERVICE_ORDER = ('wash_fold', 'bedding', 'comforter')   # regular clothes; bedsheets/towels/curtains; comforters


def prices(wash_fold, bedding, comforter):
    """Per-load prices from per-kg full-service (wash, dry, fold) rates, in the order of SERVICE_ORDER.

    Each washing service is charged per machine load (Service.LOAD_KG: 5 kg clothes, 4 kg bedding
    and comforters), so the load price is the per-kg rate times the load size.
    """
    per_kg = (Decimal(wash_fold), Decimal(bedding), Decimal(comforter))
    return [(name, rate * Service.LOAD_KG[name]) for name, rate in zip(SERVICE_ORDER, per_kg)]

SHOPS = [
    {
        'key': 'lavaz', 'name': "Lava'z Laundry Shop", 'tagline': 'Wash · Dry · Fold',
        'address': 'Copper Street, Brgy. Lininti-an, Cantilan, Surigao del Sur',
        'contact': '0915-401-7520', 'owner_first': 'Allan', 'owner_last': 'Vaz',
        'lat': 9.335092, 'lng': 125.978327, 'pin': 'street', 'photo': 'lavaz laundry shop.jpg',
        'prices': prices('30.00', '45.00', '65.00'), 'hours': (time(8, 0), time(18, 0)),   # real (survey)
    },
    {
        'key': 'crossingspin', 'name': 'Crossing Spin Laundry Shop', 'tagline': 'Wash · Dry · Fold',
        'address': 'Purok 5, Brgy. Pag-antayan, Cantilan, Surigao del Sur',
        'contact': '', 'owner_first': 'Crossing Spin', 'owner_last': 'Owner',
        'lat': 9.332018, 'lng': 125.967545, 'pin': 'barangay', 'photo': 'crossing spin laundry shop.jpg',
        'prices': prices('28.00', '40.00', '60.00'), 'hours': (time(7, 0), time(19, 0)),   # placeholder
    },
    {
        'key': 'bubblebox', 'name': 'Daily BubbleBox Laundry Hub', 'tagline': 'Laundry, GCash, bills payment',
        'address': 'Purok 2, La Purisima Street, Brgy. Lininti-an, Cantilan, Surigao del Sur',
        'contact': '', 'owner_first': 'Daily BubbleBox', 'owner_last': 'Owner',
        'lat': 9.335984, 'lng': 125.976813, 'pin': 'street', 'photo': 'DAILY BUBBLEBOX Laundry Hub.jpg',
        'prices': prices('32.00', '45.00', '70.00'), 'hours': (time(8, 0), time(20, 0)),   # placeholder
    },
    {
        'key': 'frankie', 'name': 'Frankie Laundry Shop', 'tagline': 'Wash · Dry & Fold',
        'address': 'Purok 4, Orillaneda Street, Brgy. Lininti-an, Cantilan, Surigao del Sur',
        'contact': '0999-929-2571', 'owner_first': 'Frankie', 'owner_last': 'Owner',
        'lat': 9.336422, 'lng': 125.978237, 'pin': 'street', 'photo': 'frankie laundry shop.jpg',
        'prices': prices('35.00', '50.00', '75.00'), 'hours': (time(8, 30), time(17, 30)),  # placeholder
    },
    {
        'key': 'labadana', 'name': 'Labada Na Laundry Shop', 'tagline': 'Wash · Dry · Fold',
        'address': 'Purok 3, Brgy. Lininti-an, Cantilan, Surigao del Sur',
        'contact': '', 'owner_first': 'Labada Na', 'owner_last': 'Owner',
        'lat': 9.337220, 'lng': 125.978256, 'pin': 'barangay', 'photo': 'labada na laundry shop.jpg',
        'prices': prices('25.00', '38.00', '55.00'), 'hours': (time(7, 30), time(18, 0)),   # placeholder
    },
]


def demo_email(key):
    return f'owner.{key}@clefresh.test'


def new_password(key):
    return f'{key.capitalize()}-{secrets.token_hex(3)}!'


class Command(BaseCommand):
    help = "Create the five Cantilan laundry shops (Lava'z real pricing, varied placeholders for the rest) with demo logins."

    def add_arguments(self, parser):
        parser.add_argument('--reset-passwords', action='store_true',
                            help='Give every demo owner a new password (written to the logins file).')

    def handle(self, *args, reset_passwords=False, **options):
        User = get_user_model()
        logins = []
        for spec in SHOPS:
            with transaction.atomic():
                email = demo_email(spec['key'])
                user = User.objects.filter(email__iexact=email).first()
                created_user = user is None
                if created_user:
                    user = User(username=email, email=email)
                user.first_name, user.last_name = spec['owner_first'], spec['owner_last']
                user.role, user.email_verified, user.is_active = 'shop_owner', True, True
                user.contact_number = spec['contact']
                password = None
                if created_user or reset_passwords:
                    password = new_password(spec['key'])
                    user.set_password(password)
                user.save()

                shop = Shop.objects.filter(owner=user).first() or Shop(owner=user)
                shop.name, shop.tagline, shop.address = spec['name'], spec['tagline'], spec['address']
                shop.contact = spec['contact']
                shop.latitude, shop.longitude = spec['lat'], spec['lng']     # set, so save() won't re-geocode
                shop.operating_hours_start, shop.operating_hours_end = spec['hours']
                shop.status = 'approved'
                shop.payments = ['Cash', 'GCash']
                photo = DATA_DIR / spec['photo']
                if photo.exists() and not shop.banner:
                    with photo.open('rb') as fh:
                        shop.banner.save(photo.name, File(fh), save=False)
                    with photo.open('rb') as fh:
                        shop.logo.save(photo.name, File(fh), save=False)
                shop.save()

                for name, price in spec['prices']:
                    Service.objects.update_or_create(shop=shop, name=name,
                                                     defaults={'price_per_load': price, 'load_kg': Service.LOAD_KG[name],
                                                               'price_per_kg': None, 'price_per_piece': None})
            note = 'pin: street' if spec['pin'] == 'street' else 'pin: barangay centre (approximate)'
            self.stdout.write(f"{'Created' if created_user else 'Updated'} {spec['name']} ({email}, {note})")
            if password:
                logins.append((spec['name'], email, password))

        if logins:
            DATA_DIR.mkdir(exist_ok=True)
            lines = ['CLEFRESH demo shop-owner logins (local/demo use only - do not commit or share)', '']
            lines += [f'{name}\n  email:    {email}\n  password: {pw}\n' for name, email, pw in logins]
            existing = LOGINS_FILE.read_text(encoding='utf-8') if LOGINS_FILE.exists() and not reset_passwords else ''
            body = '\n'.join(lines) if not existing else existing.rstrip() + '\n\n' + '\n'.join(lines[2:])
            LOGINS_FILE.write_text(body, encoding='utf-8')
            self.stdout.write(self.style.SUCCESS(f'{len(logins)} new login(s) written to {LOGINS_FILE}'))
        else:
            self.stdout.write(self.style.SUCCESS('Shops up to date; existing passwords kept.'))
