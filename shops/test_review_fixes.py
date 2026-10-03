"""Regression tests for the fixes from the September 2026 code review."""
import hashlib
import hmac
import json
import tempfile
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from orders.models import Order, OrderService, PhotoProof
from payments.models import PayMongoTransaction, WebhookEvent
from shops.models import DeliverySlot, Service, Shop

User = get_user_model()

# Background email threads write EmailLog concurrently and lock SQLite in tests.
_no_email_thread = mock.patch('utils.email_notifications.threading.Thread')


def _png():
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new('RGB', (1, 1)).save(buf, format='PNG')
    return SimpleUploadedFile('p.png', buf.getvalue(), content_type='image/png')


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix='clefresh-test-media-'))
class ReviewFixTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        _no_email_thread.start()

    @classmethod
    def tearDownClass(cls):
        _no_email_thread.stop()
        super().tearDownClass()

    def setUp(self):
        mk = lambda email, role, **kw: User.objects.create_user(
            username=email, email=email, password='Pass1234!', role=role, email_verified=True, **kw
        )
        self.customer = mk('c@example.com', 'customer')
        self.owner = mk('o@example.com', 'shop_owner')
        self.rider = mk('r@example.com', 'rider')
        self.admin = mk('a@example.com', 'admin')
        self.shop = Shop.objects.create(owner=self.owner, name='Shop', address='Addr', status='approved')
        self.service = Service.objects.create(shop=self.shop, name='wash', price_per_kg='50.00')
        self.iron = Service.objects.create(shop=self.shop, name='iron', price_per_piece='15.00')
        self.slot = DeliverySlot.objects.create(
            shop=self.shop, date=timezone.localdate() + timedelta(days=1),
            time_start='09:00', time_end='11:00', max_capacity=3, current_bookings=1,
        )

    def _order(self, **kw):
        defaults = dict(
            customer=self.customer, shop=self.shop, pickup_address='P', delivery_address='D',
            pickup_datetime=timezone.now() + timedelta(days=1), payment_method='cod', total_amount='100.00',
        )
        defaults.update(kw)
        return Order.objects.create(**defaults)

    # ── authorization ──
    def test_roleless_user_cannot_view_arbitrary_order(self):
        order = self._order()
        roleless = User.objects.create_user(username='x@example.com', email='x@example.com', password='Pass1234!')
        self.client.force_login(roleless)
        self.assertEqual(self.client.get(reverse('order_detail', args=[order.id])).status_code, 404)

    def test_admin_can_still_view_order(self):
        order = self._order()
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(reverse('order_detail', args=[order.id])).status_code, 200)

    # ── XSS ──
    def test_analytics_insight_escapes_shop_name(self):
        self.shop.name = '<script>alert(1)</script>'
        self.shop.save()
        self._order(payment_status='paid')
        self.client.force_login(self.admin)
        resp = self.client.get(reverse('admin_analytics'))
        self.assertNotContains(resp, '<script>alert(1)</script>')
        self.assertContains(resp, '&lt;script&gt;alert(1)&lt;/script&gt;')

    # ── admin ──
    def test_admin_can_delete_rider_with_photo_proofs(self):
        order = self._order(rider=self.rider)
        PhotoProof.objects.create(order=order, photo='proof_photos/x.jpg', proof_type='pickup', uploaded_by=self.rider)
        self.client.force_login(self.admin)
        resp = self.client.post(reverse('admin_delete_user', args=[self.rider.id]))
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(User.objects.filter(id=self.rider.id).exists())
        self.assertTrue(PhotoProof.objects.filter(order=order).exists())

    def test_admin_cannot_deactivate_self(self):
        self.client.force_login(self.admin)
        self.client.post(reverse('admin_toggle_user', args=[self.admin.id]))
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_active)

    def test_admin_transactions_show_pesos_not_centavos(self):
        order = self._order(payment_method='gcash')
        PayMongoTransaction.objects.create(order=order, amount=12345, status='succeeded')
        self.client.force_login(self.admin)
        resp = self.client.get(reverse('admin_transactions'))
        self.assertContains(resp, '₱123.45')

    # ── shop owner ──
    def test_deleting_ordered_service_keeps_order_history(self):
        order = self._order()
        OrderService.objects.create(order=order, service=self.service, weight_kg='2', subtotal='100')
        self.client.force_login(self.owner)
        self.client.post(reverse('delete_service', args=[self.service.id]))
        self.assertTrue(Service.objects.filter(id=self.service.id).exists())
        self.assertEqual(order.order_services.count(), 1)

    def test_admin_shop_delete_still_cascades(self):
        order = self._order()
        OrderService.objects.create(order=order, service=self.service, weight_kg='2', subtotal='100')
        self.client.force_login(self.admin)
        self.client.post(reverse('admin_shop_action', args=[self.shop.id]), {'action': 'delete'})
        self.assertFalse(Shop.objects.filter(id=self.shop.id).exists())

    def test_delete_slot_requires_post(self):
        self.client.force_login(self.owner)
        self.client.get(reverse('delete_slot', args=[self.slot.id]))
        self.assertTrue(DeliverySlot.objects.filter(id=self.slot.id).exists())

    def test_decline_releases_slot_booking(self):
        order = self._order(delivery_slot=self.slot)
        self.client.force_login(self.owner)
        self.client.post(reverse('update_order_status', args=[order.id]), {'status': 'declined'})
        self.slot.refresh_from_db()
        self.assertEqual(self.slot.current_bookings, 0)

    def test_status_dropdown_lists_only_valid_next_steps(self):
        order = self._order(status='washing')
        self.client.force_login(self.owner)
        resp = self.client.get(reverse('shop_dashboard_order_detail', args=[order.id]))
        self.assertEqual([v for v, _ in resp.context['next_statuses']], ['drying', 'folding', 'ready'])

    def test_assign_rider_with_bad_id_does_not_500(self):
        order = self._order(status='accepted')
        self.client.force_login(self.owner)
        resp = self.client.post(reverse('assign_rider', args=[order.id]), {'rider_id': 'abc'})
        self.assertEqual(resp.status_code, 302)

    def test_suspended_owner_cannot_resubmit_to_pending(self):
        self.shop.status = 'suspended'
        self.shop.save()
        self.client.force_login(self.owner)
        self.client.post(reverse('register_shop'), {'name': 'New', 'address': 'Somewhere'})
        self.shop.refresh_from_db()
        self.assertEqual(self.shop.status, 'suspended')

    def test_negative_service_price_rejected(self):
        self.service.delete()
        self.client.force_login(self.owner)
        self.client.post(reverse('create_service'), {'name': 'wash', 'price_per_kg': '-5'})
        self.assertFalse(self.shop.services.filter(name='wash').exists())

    def test_customer_price_sort_has_no_duplicate_shops(self):
        self.client.force_login(self.customer)
        resp = self.client.get(reverse('home'), {'sort': 'price'})
        ids = [s.id for s in resp.context['customer_shops']]
        self.assertEqual(ids.count(self.shop.id), 1)

    # ── ordering ──
    def _place(self, **data):
        base = {
            'pickup_address': 'P', 'delivery_address': 'D',
            'pickup_datetime': (timezone.localtime() + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M'),
            'payment_method': 'cod',
        }
        base.update(data)
        self.client.force_login(self.customer)
        return self.client.post(reverse('place_order', args=[self.shop.id]), base)

    def test_zero_total_order_rejected(self):
        # weight entered for a per-piece-only service -> subtotal 0
        self._place(**{f'weight_{self.iron.id}': '3'})
        self.assertFalse(Order.objects.exists())

    def test_non_numeric_slot_does_not_500(self):
        resp = self._place(delivery_slot='abc', **{f'weight_{self.service.id}': '2'})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(Order.objects.exists())

    def test_unsupported_payment_method_rejected(self):
        self._place(payment_method='card', **{f'weight_{self.service.id}': '2'})
        self.assertFalse(Order.objects.exists())

    # ── accounts ──
    def test_rider_onboarding_bad_numbers_do_not_500(self):
        self.client.force_login(self.rider)
        resp = self.client.post(reverse('rider_onboarding'), {'vehicle_year': 'abc', 'date_of_birth': 'nope'})
        self.assertEqual(resp.status_code, 200)

    def test_password_reset_enforces_strength(self):
        from django.contrib.auth.tokens import default_token_generator
        from django.utils.encoding import force_bytes
        from django.utils.http import urlsafe_base64_encode
        uid = urlsafe_base64_encode(force_bytes(self.customer.pk))
        token = default_token_generator.make_token(self.customer)
        url = reverse('password_reset_confirm', args=[uid, token])
        self.client.post(url, {'password': 'alllowercase', 'confirm_password': 'alllowercase'})
        self.customer.refresh_from_db()
        self.assertTrue(self.customer.check_password('Pass1234!'))

    def test_forgot_password_duplicate_emails_does_not_500(self):
        User.objects.create_user(username='dup', email='c@example.com', password='x')
        resp = self.client.post(reverse('forgot_password'), {'email': 'c@example.com'})
        self.assertEqual(resp.status_code, 302)

    # ── rider ──
    def test_upload_proof_get_redirects(self):
        order = self._order(rider=self.rider, status='accepted')
        self.client.force_login(self.rider)
        self.assertEqual(self.client.get(reverse('upload_proof', args=[order.id])).status_code, 302)

    def test_upload_proof_type_must_match_step(self):
        order = self._order(rider=self.rider, status='accepted')
        self.client.force_login(self.rider)
        self.client.post(reverse('upload_proof', args=[order.id]), {'proof_type': 'delivery', 'photo': _png()})
        self.assertFalse(order.photo_proofs.exists())

    def test_dropoff_proof_allowed_when_shop_already_at_ready(self):
        order = self._order(rider=self.rider, status='ready')
        self.client.force_login(self.rider)
        self.client.post(reverse('upload_proof', args=[order.id]), {'proof_type': 'dropoff', 'photo': _png()})
        self.assertTrue(order.photo_proofs.filter(proof_type='dropoff').exists())

    def test_delivery_proof_upload_records_cod(self):
        order = self._order(rider=self.rider, status='out_for_delivery')
        self.client.force_login(self.rider)
        self.client.post(
            reverse('upload_proof', args=[order.id]),
            {'proof_type': 'delivery', 'photo': _png(), 'cod_collected': 'on'},
        )
        order.refresh_from_db()
        self.assertEqual((order.status, order.payment_status), ('delivered', 'paid'))

    # ── smoke: every GET page renders (200/302, never 500) per role ──
    def test_all_pages_render_per_role(self):
        order = self._order(rider=self.rider, status='accepted', delivery_slot=self.slot, payment_method='gcash')
        OrderService.objects.create(order=order, service=self.service, weight_kg='2', subtotal='100')
        public = [reverse('home'), reverse('login'), reverse('register'), reverse('forgot_password'),
                  reverse('shop_detail', args=[self.shop.id]), reverse('shop_site_services', args=[self.shop.id]),
                  reverse('shop_site_how_it_works', args=[self.shop.id]), reverse('shop_site_about', args=[self.shop.id]),
                  reverse('info_page', args=['help']), reverse('info_page', args=['user-guide'])]
        pages = {
            None: public,
            self.customer: public + [reverse('order_list'), reverse('order_detail', args=[order.id]),
                                     reverse('place_order', args=[self.shop.id]), reverse('profile'),
                                     reverse('payment_checkout', args=[order.id]), reverse('api_slots', args=[self.shop.id])],
            self.owner: [reverse(n) for n in ('shop_dashboard', 'shop_dashboard_orders', 'shop_dashboard_slots',
                                              'shop_dashboard_services', 'shop_dashboard_analytics',
                                              'shop_dashboard_payments', 'edit_shop')]
                        + [reverse('shop_dashboard_order_detail', args=[order.id]), reverse('order_detail', args=[order.id])],
            self.rider: [reverse('rider_dashboard'), reverse('rider_task_detail', args=[order.id]),
                         reverse('rider_onboarding'), reverse('order_detail', args=[order.id])],
            self.admin: [reverse(n) for n in ('admin_dashboard', 'admin_shops', 'admin_users', 'admin_orders',
                                              'admin_transactions', 'admin_analytics')]
                        + [reverse('order_list'), reverse('order_detail', args=[order.id])],
        }
        for user, urls in pages.items():
            self.client.logout()
            if user:
                self.client.force_login(user)
            for url in urls:
                with self.subTest(user=getattr(user, 'email', 'anon'), url=url):
                    self.assertIn(self.client.get(url).status_code, (200, 302))


@override_settings(PAYMONGO_WEBHOOK_SECRET='whsec_test', PAYMONGO_SECRET_KEY='sk_test')
class WebhookFixTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        _no_email_thread.start()

    @classmethod
    def tearDownClass(cls):
        _no_email_thread.stop()
        super().tearDownClass()

    def setUp(self):
        self.customer = User.objects.create_user(username='c@e.com', email='c@e.com', password='x', role='customer')
        owner = User.objects.create_user(username='o@e.com', email='o@e.com', password='x', role='shop_owner')
        shop = Shop.objects.create(owner=owner, name='S', address='A', status='approved')
        self.order = Order.objects.create(
            customer=self.customer, shop=shop, pickup_address='P', delivery_address='D',
            pickup_datetime=timezone.now() + timedelta(days=1), payment_method='gcash',
            total_amount='100.00', paymongo_payment_intent_id='pi_1',
        )
        self.tx = PayMongoTransaction.objects.create(
            order=self.order, payment_intent_id='pi_1', amount=10000, status='awaiting_payment_method'
        )

    def _post(self, event_id, event_type):
        body = json.dumps({'data': {'id': event_id, 'attributes': {
            'type': event_type, 'data': {'id': 'pay_1', 'attributes': {'payment_intent_id': 'pi_1'}},
        }}})
        sig = hmac.new(b'whsec_test', f'1.{body}'.encode(), hashlib.sha256).hexdigest()
        return self.client.post(
            reverse('paymongo_webhook'), body, content_type='application/json',
            HTTP_PAYMONGO_SIGNATURE=f't=1,te={sig},li=',
        )

    def test_payment_failed_releases_duplicate_guard(self):
        self.assertEqual(self._post('evt_1', 'payment.failed').status_code, 200)
        self.tx.refresh_from_db()
        self.assertEqual(self.tx.status, 'failed')

    def test_failed_processing_does_not_consume_event_id(self):
        with mock.patch('payments.views._mark_order_paid', side_effect=RuntimeError):
            self.assertEqual(self._post('evt_2', 'payment.paid').status_code, 500)
        self.assertFalse(WebhookEvent.objects.filter(event_id='evt_2').exists())
        # PayMongo's retry is now processed
        self.assertEqual(self._post('evt_2', 'payment.paid').status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_status, 'paid')

    def test_abandoned_intent_no_longer_blocks_retry(self):
        self.client.force_login(self.customer)
        with mock.patch('payments.views._fetch_payment_intent_status', return_value='awaiting_payment_method'), \
                mock.patch('payments.views.requests.post', side_effect=RuntimeError('stop before network')):
            resp = self.client.post(reverse('payment_process', args=[self.order.id]), {'payment_method_type': 'gcash'})
        # Guard released: we reached the PayMongo call (which the mock aborts) instead of "already in progress".
        self.tx.refresh_from_db()
        self.assertEqual(self.tx.status, 'failed')
        self.assertRedirects(resp, reverse('payment_checkout', args=[self.order.id]), fetch_redirect_response=False)



@override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix='clefresh-test-media-'))
class RiderApprovalTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        _no_email_thread.start()

    @classmethod
    def tearDownClass(cls):
        _no_email_thread.stop()
        super().tearDownClass()

    def setUp(self):
        from accounts.models import RiderProfile
        mk = lambda email, role: User.objects.create_user(
            username=email, email=email, password='Pass1234!', role=role, email_verified=True
        )
        self.customer = mk('c@example.com', 'customer')
        self.owner = mk('o@example.com', 'shop_owner')
        self.admin = mk('a@example.com', 'admin')
        self.approved = mk('ok@example.com', 'rider')
        self.unapproved = mk('new@example.com', 'rider')
        docs = dict(government_id_front='rider_docs/a.png', government_id_back='rider_docs/b.png',
                    selfie_with_id='rider_docs/c.png')
        RiderProfile.objects.create(user=self.approved, is_submitted=True, approval_status='approved', **docs)
        self.pending_profile = RiderProfile.objects.create(
            user=self.unapproved, is_submitted=True, approval_status='pending', submitted_at=timezone.now(), **docs
        )
        self.shop = Shop.objects.create(owner=self.owner, name='Shop', address='Addr', status='approved')

    def _order(self, **kw):
        d = dict(customer=self.customer, shop=self.shop, pickup_address='P', delivery_address='D',
                 pickup_datetime=timezone.now() + timedelta(days=1), payment_method='cod', total_amount='100.00')
        d.update(kw)
        return Order.objects.create(**d)

    # ── order acceptance ──
    def test_job_board_lists_only_shop_accepted_orders(self):
        pending = self._order(status='pending')
        accepted = self._order(status='accepted')
        self.client.force_login(self.approved)
        ids = [o.id for o in self.client.get(reverse('rider_dashboard')).context['available_orders']]
        self.assertEqual(ids, [accepted.id])
        self.assertNotIn(pending.id, ids)

    def test_rider_cannot_accept_pending_order_for_shop(self):
        order = self._order(status='pending')
        self.client.force_login(self.approved)
        self.client.post(reverse('rider_accept_job', args=[order.id]))
        order.refresh_from_db()
        self.assertEqual((order.status, order.rider_id), ('pending', None))

    def test_claiming_job_assigns_rider_without_status_change(self):
        order = self._order(status='accepted')
        self.client.force_login(self.approved)
        self.client.post(reverse('rider_accept_job', args=[order.id]))
        order.refresh_from_db()
        self.assertEqual((order.status, order.rider_id), ('accepted', self.approved.id))

    def test_job_card_hides_customer_identity(self):
        self.customer.first_name = 'Zelda'
        self.customer.save()
        self._order(status='accepted')
        self.client.force_login(self.approved)
        self.assertNotContains(self.client.get(reverse('rider_dashboard')), 'Zelda')

    # ── rider approval ──
    def test_unapproved_rider_sees_no_jobs_and_cannot_claim(self):
        order = self._order(status='accepted')
        self.client.force_login(self.unapproved)
        resp = self.client.get(reverse('rider_dashboard'))
        self.assertEqual(list(resp.context['available_orders']), [])
        self.assertContains(resp, 'Application under review')
        self.client.post(reverse('rider_accept_job', args=[order.id]))
        order.refresh_from_db()
        self.assertIsNone(order.rider_id)

    def test_shop_can_only_assign_approved_riders(self):
        order = self._order(status='accepted')
        self.client.force_login(self.owner)
        resp = self.client.get(reverse('shop_dashboard_order_detail', args=[order.id]))
        self.assertEqual(list(resp.context['riders']), [self.approved])
        self.client.post(reverse('assign_rider', args=[order.id]), {'rider_id': self.unapproved.id})
        order.refresh_from_db()
        self.assertIsNone(order.rider_id)
        self.client.post(reverse('assign_rider', args=[order.id]), {'rider_id': self.approved.id})
        order.refresh_from_db()
        self.assertEqual(order.rider_id, self.approved.id)

    def test_admin_approves_rider(self):
        self.client.force_login(self.admin)
        resp = self.client.get(reverse('admin_riders'))
        self.assertContains(resp, self.unapproved.email)
        self.client.post(reverse('admin_rider_action', args=[self.pending_profile.id]), {'action': 'approve'})
        self.pending_profile.refresh_from_db()
        self.assertEqual(self.pending_profile.approval_status, 'approved')
        self.assertTrue(self.unapproved.notifications.exists())

    def test_non_admin_cannot_approve(self):
        self.client.force_login(self.unapproved)
        self.client.post(reverse('admin_rider_action', args=[self.pending_profile.id]), {'action': 'approve'})
        self.pending_profile.refresh_from_db()
        self.assertEqual(self.pending_profile.approval_status, 'pending')

    def test_revoking_releases_unstarted_jobs_only(self):
        unstarted = self._order(status='accepted', rider=self.approved)
        in_progress = self._order(status='picked_up', rider=self.approved)
        profile = self.approved.rider_profile
        self.client.force_login(self.admin)
        self.client.post(reverse('admin_rider_action', args=[profile.id]), {'action': 'reject', 'reason': 'Expired ID'})
        unstarted.refresh_from_db(); in_progress.refresh_from_db(); profile.refresh_from_db()
        self.assertEqual(profile.approval_status, 'rejected')
        self.assertIsNone(unstarted.rider_id)
        self.assertEqual(in_progress.rider_id, self.approved.id)

    def test_rejected_rider_resubmission_returns_to_pending(self):
        self.pending_profile.approval_status = 'rejected'
        self.pending_profile.save()
        self.client.force_login(self.unapproved)
        self.client.post(reverse('rider_onboarding'), {'first_name': 'New'})
        self.pending_profile.refresh_from_db()
        self.assertEqual(self.pending_profile.approval_status, 'pending')

    def test_approved_rider_new_documents_require_review(self):
        self.client.force_login(self.approved)
        self.client.post(reverse('rider_onboarding'), {'shift_start': '08:00'})
        self.assertEqual(self.approved.rider_profile.__class__.objects.get(user=self.approved).approval_status, 'approved')
        self.client.post(reverse('rider_onboarding'), {'vehicle_or': _png()})
        self.assertEqual(self.approved.rider_profile.__class__.objects.get(user=self.approved).approval_status, 'pending')


@override_settings(MEDIA_ROOT=tempfile.mkdtemp(prefix='clefresh-test-media-'))
class UiUxFixTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        _no_email_thread.start()

    @classmethod
    def tearDownClass(cls):
        _no_email_thread.stop()
        super().tearDownClass()

    def setUp(self):
        from accounts.models import RiderProfile
        mk = lambda email, role: User.objects.create_user(
            username=email, email=email, password='Pass1234!', role=role, email_verified=True
        )
        self.customer = mk('c@example.com', 'customer')
        self.owner = mk('o@example.com', 'shop_owner')
        self.admin = mk('a@example.com', 'admin')
        self.rider = mk('r@example.com', 'rider')
        self.profile = RiderProfile.objects.create(user=self.rider)
        self.shop = Shop.objects.create(owner=self.owner, name='Suds <b>& Co</b>', address='Tandag', status='approved')
        self.service = Service.objects.create(shop=self.shop, name='wash', price_per_kg='40.00')

    def _place(self, **data):
        base = {'pickup_address': 'P', 'delivery_address': 'D', 'payment_method': 'cod',
                'pickup_datetime': (timezone.localtime() + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M'),
                f'weight_{self.service.id}': '2'}
        base.update(data)
        self.client.force_login(self.customer)
        return self.client.post(reverse('place_order', args=[self.shop.id]), base)

    # ── booking page ──
    def test_addons_are_saved_to_order_notes(self):
        self._place(addons=['express', 'hypoallergenic', 'bogus'], notes='Fold shirts')
        notes = Order.objects.get().notes
        self.assertIn('Express service requested', notes)
        self.assertIn('Use hypoallergenic detergent', notes)
        self.assertIn('Fold shirts', notes)
        self.assertNotIn('bogus', notes)

    def test_rejected_booking_echoes_values_and_step(self):
        resp = self._place(pickup_datetime='2000-01-01T10:00', addons=['express'], notes='keep me')
        self.assertEqual(resp.context['error_step'], 3)
        self.assertEqual(resp.context['form_data']['notes'], 'keep me')
        self.assertEqual(resp.context['form_data']['addons'], ['express'])

    # ── shop list ──
    def test_public_shop_list_renders_with_search(self):
        resp = self.client.get(reverse('shop_list'), {'q': 'Tandag'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual([s.id for s in resp.context['shops']], [self.shop.id])
        self.assertEqual(self.client.get(reverse('shop_list'), {'q': 'nomatch'}).context['shops'], [])

    def test_customer_shop_list_goes_to_home_list(self):
        self.client.force_login(self.customer)
        self.assertRedirects(self.client.get(reverse('shop_list')), reverse('home') + '#shops',
                             fetch_redirect_response=False)

    def test_shop_list_markers_are_json_escaped(self):
        self.shop.latitude, self.shop.longitude = 9.0, 126.0
        self.shop.save()
        html = self.client.get(reverse('shop_list')).content.decode()
        self.assertIn('id="shop-markers"', html)
        self.assertNotIn('<b>& Co</b>', html)

    # ── shop owner documents ──
    def test_register_shop_stores_owner_documents(self):
        self.shop.status = 'rejected'
        self.shop.save()
        self.client.force_login(self.owner)
        self.client.post(reverse('register_shop'), {
            'name': 'Suds', 'address': 'Tandag', 'government_id': _png(), 'selfie_id': _png(),
        })
        self.shop.refresh_from_db()
        self.assertTrue(self.shop.owner_government_id)
        self.assertTrue(self.shop.owner_selfie_with_id)

    def test_owner_documents_visible_to_owner_and_admin_only(self):
        from django.core.files.base import ContentFile
        self.shop.owner_government_id.save('id.png', ContentFile(_png().read()))
        url = reverse('serve_shop_doc', args=[self.shop.id, 'owner_government_id'])
        for user, expected in ((self.owner, 200), (self.admin, 200), (self.customer, 404), (self.rider, 404)):
            self.client.force_login(user)
            self.assertEqual(self.client.get(url).status_code, expected, user.email)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(reverse('serve_shop_doc', args=[self.shop.id, 'logo'])).status_code, 404)

    # ── rider onboarding ──
    def test_onboarding_requires_documents_server_side(self):
        self.client.force_login(self.rider)
        self.client.post(reverse('rider_onboarding'), {'first_name': 'R', 'vehicle_type': 'motorcycle'})
        self.profile.refresh_from_db()
        self.assertFalse(self.profile.is_submitted)

    def test_onboarding_address_not_duplicated_on_resave(self):
        self.client.force_login(self.rider)
        self.client.post(reverse('rider_onboarding'), {
            'first_name': 'R', 'vehicle_type': 'bicycle', 'home_address': '12 Rizal St, Brgy 1',
            'city': 'Tandag', 'province': 'Surigao del Sur',
            'government_id_front': _png(), 'government_id_back': _png(), 'selfie_with_id': _png(),
        })
        ctx = self.client.get(reverse('rider_onboarding')).context
        self.assertEqual((ctx['addr_home'], ctx['addr_city'], ctx['addr_province']),
                         ('12 Rizal St, Brgy 1', 'Tandag', 'Surigao del Sur'))
        # Re-save the pre-filled form without new files: address unchanged, documents kept.
        self.client.post(reverse('rider_onboarding'), {
            'first_name': 'R', 'vehicle_type': 'bicycle', 'home_address': ctx['addr_home'],
            'city': ctx['addr_city'], 'province': ctx['addr_province'],
        })
        self.rider.refresh_from_db()
        self.assertEqual(self.rider.address, '12 Rizal St, Brgy 1, Tandag, Surigao del Sur')

    def test_onboarding_prefills_saved_choices(self):
        self.profile.is_submitted = True
        self.profile.vehicle_type = 'car'
        self.profile.available_days = 'Sun'
        self.profile.coverage_areas = 'Tandag'
        self.profile.accepts_cod = False
        self.profile.save()
        self.client.force_login(self.rider)
        ctx = self.client.get(reverse('rider_onboarding')).context
        self.assertEqual(ctx['selected_vehicle'], 'car')
        self.assertEqual(ctx['selected_days'], {'Sun'})
        self.assertEqual(ctx['selected_zones'], {'Tandag'})
        self.assertFalse(ctx['pref_cod'])
