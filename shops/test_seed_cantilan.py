"""seed_cantilan_shops: five approved shops with Lava'z pricing, 8 AM-6 PM, and working demo logins."""
import io
import re
import shutil
import tempfile
from decimal import Decimal
from pathlib import Path
from unittest import mock

from django.contrib.auth import authenticate
from django.core.management import call_command
from django.test import TestCase, override_settings

from shops.management.commands import seed_cantilan_shops as cmd
from shops.models import Shop

_MEDIA = tempfile.mkdtemp()


@override_settings(MEDIA_ROOT=_MEDIA)
class SeedCantilanShopsTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_MEDIA, ignore_errors=True)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.logins = self.tmp / 'demo_logins.txt'
        patcher = mock.patch.object(cmd, 'LOGINS_FILE', self.logins)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_creates_five_shops_lavaz_real_others_varied(self):
        call_command('seed_cantilan_shops', stdout=io.StringIO())
        shops = Shop.objects.filter(owner__email__endswith='@clefresh.test')
        self.assertEqual(shops.count(), 5)
        price_lists, hours = set(), set()
        for shop in shops:
            self.assertEqual(shop.status, 'approved')
            self.assertIsNotNone(shop.latitude)
            prices = dict(shop.services.values_list('name', 'price_per_load'))
            self.assertFalse(shop.services.filter(price_per_kg__isnull=False).exists())
            self.assertEqual(set(prices), {'wash_fold', 'bedding', 'comforter'})
            price_lists.add(tuple(sorted(prices.items())))
            hours.add(shop.hours_label)
        self.assertEqual(len(price_lists), 5)          # no two shops look the same
        self.assertGreater(len(hours), 1)
        lavaz = shops.get(name="Lava'z Laundry Shop")
        self.assertEqual(lavaz.hours_label, '8:00 AM – 6:00 PM')
        self.assertEqual(dict(lavaz.services.values_list('name', 'price_per_load')),
                         {'wash_fold': Decimal('150.00'), 'bedding': Decimal('180.00'), 'comforter': Decimal('260.00')})
        self.assertEqual(dict(lavaz.services.values_list('name', 'load_kg')),
                         {'wash_fold': Decimal('5.00'), 'bedding': Decimal('4.00'), 'comforter': Decimal('4.00')})

    def test_logins_work_and_rerun_keeps_passwords(self):
        call_command('seed_cantilan_shops', stdout=io.StringIO())
        pairs = re.findall(r'email:\s+(\S+)\s+password:\s+(\S+)', self.logins.read_text(encoding='utf-8'))
        self.assertEqual(len(pairs), 5)
        for email, pw in pairs:
            self.assertEqual(authenticate(username=email, password=pw).role, 'shop_owner')
        out = io.StringIO()
        call_command('seed_cantilan_shops', stdout=out)
        self.assertIn('existing passwords kept', out.getvalue())
        self.assertEqual(Shop.objects.filter(owner__email__endswith='@clefresh.test').count(), 5)
        email, pw = pairs[0]
        self.assertIsNotNone(authenticate(username=email, password=pw))
