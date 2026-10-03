"""Regression tests for the security/workflow audit fixes."""
from datetime import timedelta, time
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import RiderProfile
from notifications.models import Notification
from orders.models import Order, OrderStatusLog
from payments.models import PayMongoTransaction, Refund
from payments.views import _mark_order_paid
from shops.models import DeliverySlot, Service, Shop

User = get_user_model()


def _resp(status, payload):
    r = mock.Mock(status_code=status)
    r.json.return_value = payload
    return r


@override_settings(PAYMONGO_SECRET_KEY='sk_test')
class AuditFixTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._thread_patch = mock.patch('utils.email_notifications.threading.Thread')
        cls._thread_patch.start()

    @classmethod
    def tearDownClass(cls):
        cls._thread_patch.stop()
        super().tearDownClass()

    def setUp(self):
        mk = lambda email, role, **kw: User.objects.create_user(
            username=email, email=email, password='Pass1234!', role=role, email_verified=True, **kw)
        self.customer = mk('c@e.com', 'customer')
        self.owner = mk('o@e.com', 'shop_owner')
        self.admin = mk('a@e.com', 'admin')
        self.rider = mk('r@e.com', 'rider')
        self.rider2 = mk('r2@e.com', 'rider')
        for r in (self.rider, self.rider2):
            RiderProfile.objects.create(user=r, is_submitted=True, approval_status='approved')
        self.shop = Shop.objects.create(owner=self.owner, name='Suds', address='Tandag', status='approved')
        self.wash = Service.objects.create(shop=self.shop, name='wash', price_per_kg='50.00')

    def _order(self, **kw):
        fields = dict(customer=self.customer, shop=self.shop, pickup_address='P', delivery_address='D',
                      pickup_datetime=timezone.now() + timedelta(days=1), payment_method='gcash',
                      total_amount='200.00', status='pending')
        fields.update(kw)
        return Order.objects.create(**fields)

    def _paid(self, **kw):
        order = self._order(payment_status='paid', **kw)
        PayMongoTransaction.objects.create(order=order, payment_intent_id='pi_1', payment_id='pay_1',
                                           amount=20000, status='succeeded', payment_method_type='gcash')
        return order

    # ── paid orders that get cancelled/declined are refunded ──
    def test_cancelling_a_paid_order_refunds_it(self):
        order = self._paid()
        self.client.force_login(self.customer)
        with mock.patch('payments.views.requests.post', return_value=_resp(200, {'data': {'id': 'ref_1'}})) as post:
            self.client.post(reverse('cancel_order', args=[order.id]))
        order.refresh_from_db()
        self.assertEqual(order.status, 'cancelled')
        self.assertEqual(post.call_args.kwargs['json']['data']['attributes']['amount'], 20000)   # centavos
        self.assertTrue(Refund.objects.filter(order=order, status='pending').exists())

    def test_declining_a_paid_order_refunds_it(self):
        order = self._paid()
        self.client.force_login(self.owner)
        with mock.patch('payments.views.requests.post', return_value=_resp(200, {'data': {'id': 'ref_1'}})):
            self.client.post(reverse('update_order_status', args=[order.id]), {'status': 'declined'})
        order.refresh_from_db()
        self.assertEqual(order.status, 'declined')
        self.assertEqual(Refund.objects.filter(order=order).count(), 1)

    def test_failed_auto_refund_alerts_shop_and_admin(self):
        order = self._paid()
        self.client.force_login(self.customer)
        with mock.patch('payments.views.requests.post', return_value=_resp(400, {'errors': [{'detail': 'nope'}]})):
            self.client.post(reverse('cancel_order', args=[order.id]))
        self.assertTrue(Notification.objects.filter(user=self.owner, message__contains='refund').exists())
        self.assertTrue(Notification.objects.filter(user=self.admin, message__contains='refund').exists())

    def test_payment_landing_after_cancel_is_refunded(self):
        order = self._order(status='cancelled')
        PayMongoTransaction.objects.create(order=order, payment_intent_id='pi_9', amount=20000, status='processing')
        with mock.patch('payments.views.requests.post', return_value=_resp(200, {'data': {'id': 'ref_9'}})):
            with self.captureOnCommitCallbacks(execute=True):
                _mark_order_paid(order, 'pi_9', 'pay_9')
        self.assertTrue(Refund.objects.filter(order=order).exists())

    def test_cancel_blocked_while_paying(self):
        order = self._order()
        PayMongoTransaction.objects.create(order=order, payment_intent_id='pi_1', amount=20000, status='processing')
        self.client.force_login(self.customer)
        self.client.post(reverse('cancel_order', args=[order.id]))
        order.refresh_from_db()
        self.assertEqual(order.status, 'pending')

    def test_cancel_does_not_overwrite_a_concurrent_accept(self):
        order = self._order()
        self.client.force_login(self.customer)
        real_get = Order.objects.get
        # Shop accepts between the view loading the order and cancelling it.
        with mock.patch('orders.views.get_object_or_404', side_effect=lambda *a, **k: (
                Order.objects.filter(pk=order.pk).update(status='accepted'), real_get(pk=order.pk))[1]):
            self.client.post(reverse('cancel_order', args=[order.id]))
        order.refresh_from_db()
        self.assertEqual(order.status, 'accepted')

    # ── abandoned / stale payment attempts ──
    def test_stale_attempt_stops_blocking(self):
        order = self._order(status='ready')
        tx = PayMongoTransaction.objects.create(order=order, payment_intent_id='pi_1', amount=20000,
                                                status='awaiting_payment_method')
        self.assertTrue(order.online_payment_in_progress())
        PayMongoTransaction.objects.filter(pk=tx.pk).update(updated_at=timezone.now() - timedelta(minutes=31))
        self.assertFalse(order.online_payment_in_progress())

    def test_backing_out_of_ewallet_releases_attempt(self):
        order = self._order(paymongo_payment_intent_id='pi_1')
        PayMongoTransaction.objects.create(order=order, payment_intent_id='pi_1', amount=20000,
                                           status='awaiting_payment_method')
        self.client.force_login(self.customer)
        attrs = {'data': {'attributes': {'status': 'awaiting_payment_method'}}}
        with mock.patch('payments.views.requests.get', return_value=_resp(200, attrs)):
            self.client.get(reverse('payment_return') + f'?order_id={order.id}')
        self.assertEqual(PayMongoTransaction.objects.get(order=order).status, 'failed')

    def test_paymongo_error_releases_reservation(self):
        order = self._order()
        self.client.force_login(self.customer)
        with mock.patch('payments.views.requests.post', return_value=_resp(400, {'errors': [{'detail': 'down'}]})):
            self.client.post(reverse('payment_process', args=[order.id]), {'payment_method_type': 'gcash'})
        self.assertEqual(PayMongoTransaction.objects.get(order=order).status, 'failed')

    def test_reweigh_blocked_by_payment_reservation(self):
        order = self._order(status='washing', weigh_at_shop=True, price_confirmed_at=timezone.now())
        line = order.order_services.create(service=self.wash, quantity=0, weight_kg='4', subtotal='200')
        PayMongoTransaction.objects.create(order=order, amount=20000, status='processing')
        self.client.force_login(self.owner)
        self.client.post(reverse('confirm_order_weight', args=[order.id]), {f'weight_{line.id}': '20'})
        order.refresh_from_db()
        self.assertEqual(str(order.total_amount), '200.00')

    # ── input validation ──
    def test_nan_weight_is_rejected_not_500(self):
        order = self._order(status='washing', weigh_at_shop=True)
        line = order.order_services.create(service=self.wash, quantity=0, subtotal=0)
        self.client.force_login(self.owner)
        for bad in ('NaN', 'sNaN', 'Infinity'):
            resp = self.client.post(reverse('confirm_order_weight', args=[order.id]), {f'weight_{line.id}': bad})
            self.assertEqual(resp.status_code, 302)
        from orders.views import _parse_non_negative_decimal
        with self.assertRaises(ValueError):
            _parse_non_negative_decimal('NaN', 'Weight')

    # ── concurrency guards ──
    def test_double_decline_applies_once(self):
        slot = DeliverySlot.objects.create(shop=self.shop, date=timezone.localdate() + timedelta(days=1),
                                           time_start=time(9), time_end=time(11), current_bookings=2)
        order = self._order(delivery_slot=slot)
        self.client.force_login(self.owner)
        url = reverse('update_order_status', args=[order.id])
        self.client.post(url, {'status': 'declined'})
        self.client.post(url, {'status': 'declined'})
        slot.refresh_from_db()
        self.assertEqual((slot.current_bookings, OrderStatusLog.objects.filter(order=order).count()), (1, 1))

    def test_job_claim_only_one_rider_wins(self):
        order = self._order(status='accepted')
        for r in (self.rider, self.rider2):
            self.client.force_login(r)
            self.client.post(reverse('rider_accept_job', args=[order.id]))
        order.refresh_from_db()
        self.assertEqual(order.rider, self.rider)

    def test_suspended_shop_jobs_hidden_from_riders(self):
        order = self._order(status='accepted')
        Shop.objects.filter(pk=self.shop.pk).update(status='suspended')
        self.client.force_login(self.rider)
        self.client.post(reverse('rider_accept_job', args=[order.id]))
        order.refresh_from_db()
        self.assertIsNone(order.rider)

    # ── shop / admin guards ──
    def test_slot_with_active_booking_cannot_be_deleted(self):
        slot = DeliverySlot.objects.create(shop=self.shop, date=timezone.localdate() + timedelta(days=1),
                                           time_start=time(9), time_end=time(11), current_bookings=1)
        self._order(delivery_slot=slot)
        self.client.force_login(self.owner)
        self.client.post(reverse('delete_slot', args=[slot.id]))
        self.assertTrue(DeliverySlot.objects.filter(pk=slot.pk).exists())

    def test_admin_cannot_reinstate_a_pending_shop(self):
        Shop.objects.filter(pk=self.shop.pk).update(status='pending')
        self.client.force_login(self.admin)
        self.client.post(reverse('admin_shop_action', args=[self.shop.id]), {'action': 'unsuspend'})
        self.shop.refresh_from_db()
        self.assertEqual(self.shop.status, 'pending')

    # ── auth ──
    def test_logout_requires_post(self):
        self.client.force_login(self.customer)
        self.client.get(reverse('logout'))
        self.assertIn('_auth_user_id', self.client.session)
        self.client.post(reverse('logout'))
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_allauth_local_account_pages_redirect(self):
        self.assertRedirects(self.client.get('/accounts/signup/'), reverse('register'), fetch_redirect_response=False)
        self.assertRedirects(self.client.get('/accounts/password/reset/'), reverse('forgot_password'),
                             fetch_redirect_response=False)
        self.client.force_login(self.customer)
        self.assertRedirects(self.client.get('/accounts/email/'), reverse('profile'), fetch_redirect_response=False)

    def test_sso_completion_needs_a_google_account(self):
        u = User.objects.create_user(username='x@e.com', email='x@e.com', password='Pass1234!', role='')
        self.client.force_login(u)
        self.client.post(reverse('sso_complete'), {'role': 'shop_owner'})
        u.refresh_from_db()
        self.assertEqual((u.role, u.email_verified), ('', False))

    def test_shop_can_skip_processing_steps_to_ready(self):
        order = self._order(status='picked_up', payment_method='cod')
        self.client.force_login(self.owner)
        page = self.client.get(reverse('shop_dashboard_order_detail', args=[order.id]))
        self.assertEqual([v for v, _ in page.context['next_statuses']], ['washing', 'drying', 'folding', 'ready'])
        self.client.post(reverse('update_order_status', args=[order.id]), {'status': 'ready'})
        order.refresh_from_db()
        self.assertEqual(order.status, 'ready')
        self.client.post(reverse('update_order_status', args=[order.id]), {'status': 'pending'})   # never backwards
        order.refresh_from_db()
        self.assertEqual(order.status, 'ready')

    # ── cash on delivery is collected, not forgotten ──
    def _out_for_delivery_cod(self):
        from orders.models import PhotoProof
        order = self._order(status='out_for_delivery', payment_method='cod', rider=self.rider)
        PhotoProof.objects.create(order=order, photo='proof_photos/x.webp', proof_type='delivery', uploaded_by=self.rider)
        return order

    def test_cod_delivery_needs_cash_confirmation(self):
        order = self._out_for_delivery_cod()
        self.client.force_login(self.rider)
        url = reverse('rider_update_status', args=[order.id])
        self.client.post(url, {'status': 'delivered'})
        order.refresh_from_db()
        self.assertEqual((order.status, order.payment_status), ('out_for_delivery', 'unpaid'))
        self.client.post(url, {'status': 'delivered', 'cod_collected': 'on'})
        order.refresh_from_db()
        self.assertEqual((order.status, order.payment_status), ('delivered', 'paid'))

    def test_cash_received_for_already_delivered_cod_order(self):
        order = self._order(status='delivered', payment_method='cod', rider=self.rider)
        self.client.force_login(self.customer)                     # not the rider/shop: refused
        self.assertEqual(self.client.post(reverse('confirm_cash_received', args=[order.id])).status_code, 404)
        self.client.force_login(self.owner)
        self.client.post(reverse('confirm_cash_received', args=[order.id]))
        order.refresh_from_db()
        self.assertEqual(order.payment_status, 'paid')
