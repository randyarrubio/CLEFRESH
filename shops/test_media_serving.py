"""Without nginx (Railway), Django serves public uploads but never the private folders."""
import shutil
import tempfile
from pathlib import Path

from django.test import TestCase, override_settings

_MEDIA = Path(tempfile.mkdtemp())


@override_settings(MEDIA_ROOT=_MEDIA)
class MediaServingTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        for folder in ('shop_banners', 'id_documents', 'face_selfies', 'rider_docs', 'shop_docs'):
            (_MEDIA / folder).mkdir(parents=True, exist_ok=True)
            (_MEDIA / folder / 'file.jpg').write_bytes(b'\xff\xd8\xff' + b'0' * 32)

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_MEDIA, ignore_errors=True)

    def test_public_upload_is_served(self):
        resp = self.client.get('/media/shop_banners/file.jpg')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('max-age=86400', resp['Cache-Control'])

    def test_private_uploads_stay_blocked(self):
        for folder in ('id_documents', 'face_selfies', 'rider_docs', 'shop_docs'):
            self.assertEqual(self.client.get(f'/media/{folder}/file.jpg').status_code, 404, folder)

    def test_path_traversal_rejected(self):
        self.assertIn(self.client.get('/media/../manage.py').status_code, (400, 404))
