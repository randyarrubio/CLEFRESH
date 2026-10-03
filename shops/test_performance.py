"""Performance guards: uploads are shrunk on save, landing page query budget."""
import io
import shutil
import tempfile

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from PIL import Image

from shops.models import Service, Shop

User = get_user_model()
MEDIA = tempfile.mkdtemp()


def _png(w, h):
    buf = io.BytesIO()
    Image.effect_noise((w, h), 60).convert('RGB').save(buf, 'PNG')   # noisy = big, like a photo
    return SimpleUploadedFile('photo.png', buf.getvalue(), content_type='image/png')


@override_settings(MEDIA_ROOT=MEDIA)
class PerformanceTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(MEDIA, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        self.owner = User.objects.create_user(username='o@e.com', email='o@e.com', password='x', role='shop_owner')

    def test_uploaded_png_logo_is_downscaled_to_webp(self):
        upload = _png(3000, 2000)
        original = upload.size
        shop = Shop.objects.create(owner=self.owner, name='Suds', address='', status='approved', logo=upload)
        shop.refresh_from_db()
        self.assertTrue(shop.logo.name.startswith('shop_logos/') and shop.logo.name.endswith('.webp'))
        with Image.open(shop.logo.path) as im:
            self.assertEqual((im.format, max(im.size)), ('WEBP', 800))
        self.assertLess(shop.logo.size, original / 5)

    def test_small_upload_is_left_alone(self):
        buf = io.BytesIO()
        Image.new('RGB', (200, 200), 'navy').save(buf, 'PNG')
        shop = Shop.objects.create(owner=self.owner, name='Suds', address='', status='approved',
                                   logo=SimpleUploadedFile('logo.png', buf.getvalue(), content_type='image/png'))
        self.assertTrue(shop.logo.name.endswith('.png'))

    def test_landing_page_loads_six_shops_once(self):
        for i in range(10):
            s = Shop.objects.create(owner=self.owner, name=f'S{i}', address='', status='approved')
            Service.objects.create(shop=s, name='wash', price_per_kg='50.00')
        self.client.get('/')
        with CaptureQueriesContext(connection) as ctx:
            resp = self.client.get('/')
        self.assertEqual(len(resp.context['shops']), 6)
        sqls = [q['sql'] for q in ctx.captured_queries if 'shops_service' in q['sql']]
        self.assertEqual(len(sqls), 1)                      # services prefetched once, for 6 shops
        self.assertLessEqual(len(ctx.captured_queries), 8)
