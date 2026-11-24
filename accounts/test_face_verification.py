import io
import shutil
import tempfile
import unittest
from unittest import mock

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from PIL import Image

from notifications.models import Notification
from shops.models import Shop
from utils import face_match
from utils.face_match import FaceMatch
from .models import User

_MEDIA = tempfile.mkdtemp()


def _jpeg(color, name='frame.jpg'):
    buf = io.BytesIO()
    Image.new('RGB', (64, 48), color).save(buf, format='JPEG')
    return SimpleUploadedFile(name, buf.getvalue(), content_type='image/jpeg')


def _match(status, score=None):
    return mock.patch('accounts.views.face_match.compare', return_value=FaceMatch(status, score))


@override_settings(MEDIA_ROOT=_MEDIA)
class FaceVerificationTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_MEDIA, ignore_errors=True)

    def setUp(self):
        cache.clear()
        self.customer = User.objects.create_user(username='c', email='c@example.com', password='x', role='customer')
        self.admin = User.objects.create_user(username='a', email='a@example.com', password='x', role='admin')
        self.client.force_login(self.customer)

    def _start(self):
        self.client.get(reverse('face_verify'))
        return self.client.session['face_challenge']['nonce']

    def _submit(self, nonce, **overrides):
        data = {'nonce': nonce, 'consent': 'on', 'id_type': 'philsys',
                'selfie': _jpeg('red'), 'action_frame': _jpeg('blue'), 'id_document': _jpeg('green', 'id.jpg')}
        data.update(overrides)
        return self.client.post(reverse('face_verify'), data)

    # ── Liveness challenge ────────────────────────────────────────
    def test_challenge_has_two_distinct_actions(self):
        self._start()
        steps = self.client.session['face_challenge']['steps']
        self.assertEqual(len(set(steps)), 2)

    def test_nonce_is_single_use(self):
        nonce = self._start()
        with _match('matched', 0.7):
            self._submit(nonce)
        User.objects.filter(pk=self.customer.pk).update(face_verified_at=None, id_status='none')
        with _match('matched', 0.7):
            self.assertEqual(self._submit(nonce).status_code, 400)

    def test_wrong_nonce_rejected(self):
        self._start()
        self.assertEqual(self._submit('forged').status_code, 400)

    def test_consent_required(self):
        self.assertEqual(self._submit(self._start(), consent='').status_code, 400)

    def test_identical_frames_rejected(self):
        resp = self._submit(self._start(), selfie=_jpeg('red'), action_frame=_jpeg('red'))
        self.assertEqual(resp.status_code, 400)

    # ── ID + face match ───────────────────────────────────────────
    def test_matching_id_verifies_customer(self):
        with _match('matched', 0.71) as compare:
            resp = self._submit(self._start())
        self.assertTrue(resp.json()['ok'])
        compare.assert_called_once()
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.id_status, 'verified')
        self.assertEqual(self.customer.id_match_score, 0.71)
        self.assertEqual(self.customer.id_type, 'philsys')
        self.assertIsNotNone(self.customer.face_verified_at)
        self.assertTrue(self.customer.id_document.name.startswith('id_documents/'))
        self.assertTrue(self.customer.face_selfie.name.startswith('face_selfies/'))
        self.assertFalse(self.customer.needs_face_verification)

    def test_mismatch_goes_to_admin_review(self):
        with _match('mismatch', 0.12):
            resp = self._submit(self._start())
        self.assertTrue(resp.json()['ok'])
        self.assertEqual(resp.json()['redirect'], reverse('face_verify'))
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.id_status, 'pending_review')
        self.assertTrue(self.customer.needs_face_verification)
        self.assertTrue(Notification.objects.filter(user=self.admin, link=reverse('admin_id_reviews')).exists())
        page = self.client.get(reverse('face_verify'))
        self.assertContains(page, 'ID under review')

    def test_matcher_unavailable_falls_back_to_review(self):
        with _match('unavailable'):
            self._submit(self._start())
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.id_status, 'pending_review')
        self.assertIsNone(self.customer.id_match_score)

    def test_no_face_on_id_is_rejected_without_saving(self):
        with _match('no_face_id'):
            resp = self._submit(self._start())
        self.assertEqual(resp.status_code, 400)
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.id_status, 'none')
        self.assertFalse(self.customer.id_document)

    def test_id_photo_and_type_required(self):
        nonce = self._start()
        self.assertEqual(self._submit(nonce, id_document='').status_code, 400)
        self.assertEqual(self._submit(self._start(), id_type='library_card').status_code, 400)

    # ── Booking gate ──────────────────────────────────────────────
    def _shop(self):
        owner = User.objects.create_user(username='o', email='o@example.com', password='x', role='shop_owner')
        return Shop.objects.create(owner=owner, name='S', address='Manila', status='approved', latitude=14.6, longitude=121.0)

    def test_booking_requires_face_check(self):
        shop = self._shop()
        resp = self.client.get(reverse('place_order', args=[shop.id]))
        self.assertRedirects(resp, f"{reverse('face_verify')}?next={reverse('place_order', args=[shop.id])}",
                             fetch_redirect_response=False)

    def test_booking_blocked_while_id_pending(self):
        shop = self._shop()
        with _match('mismatch', 0.1):
            self._submit(self._start())
        resp = self.client.get(reverse('place_order', args=[shop.id]))
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp['Location'].startswith(reverse('face_verify')))

    # ── Admin review ──────────────────────────────────────────────
    def test_admin_can_approve_and_reject(self):
        with _match('mismatch', 0.2):
            self._submit(self._start())
        self.client.force_login(self.admin)
        page = self.client.get(reverse('admin_id_reviews'))
        self.assertContains(page, self.customer.email)

        self.client.post(reverse('admin_id_review_action', args=[self.customer.id]), {'action': 'approve'})
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.id_status, 'verified')
        self.assertFalse(self.customer.needs_face_verification)

        self.client.post(reverse('admin_id_review_action', args=[self.customer.id]),
                         {'action': 'reject', 'reason': 'ID is blurry'})
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.id_status, 'rejected')
        self.assertEqual(self.customer.id_rejection_reason, 'ID is blurry')
        self.assertTrue(Notification.objects.filter(user=self.customer, message__contains='ID is blurry').exists())

    def test_customer_cannot_use_admin_review(self):
        with _match('mismatch', 0.2):
            self._submit(self._start())
        self.client.post(reverse('admin_id_review_action', args=[self.customer.id]), {'action': 'approve'})
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.id_status, 'pending_review')

    # ── Private file serving ──────────────────────────────────────
    def test_identity_files_only_served_to_owner_and_admin(self):
        with _match('matched', 0.8):
            self._submit(self._start())
        self.customer.refresh_from_db()
        urls = [
            reverse('serve_face_selfie', args=[self.customer.face_selfie.name.split('/')[-1]]),
            reverse('serve_id_document', args=[self.customer.id_document.name.split('/')[-1]]),
        ]
        for url in urls:
            self.assertEqual(self.client.get(url).status_code, 200)
        other = User.objects.create_user(username='d', email='d@example.com', password='x', role='customer')
        self.client.force_login(other)
        for url in urls:
            self.assertEqual(self.client.get(url).status_code, 404)
        self.client.force_login(self.admin)
        for url in urls:
            self.assertEqual(self.client.get(url).status_code, 200)
        for field in (self.customer.face_selfie, self.customer.id_document):
            self.assertEqual(self.client.get('/media/' + field.name).status_code, 404)


class FaceMatchEngineTests(TestCase):
    """Runs the real OpenCV models (skipped where OpenCV isn't installed)."""

    @unittest.skipUnless(face_match.DETECTOR_MODEL.exists() and face_match.RECOGNIZER_MODEL.exists(), 'models missing')
    def test_blank_images_have_no_face(self):
        try:
            import cv2  # noqa: F401
        except ImportError:
            self.skipTest('OpenCV not installed')
        result = face_match.compare(_jpeg('red'), _jpeg('blue'))
        self.assertEqual(result.status, 'no_face_selfie')
