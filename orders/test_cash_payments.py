"""Cash on delivery only for trusted customers; new customers pay online or cash on pickup."""
import io
from datetime import datetime, time, timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import RiderProfile
from orders.models import Order, PhotoProof
from shops.models import Service, Shop

User = get_user_model()


def _tomorrow_at(hour):
    day = timezone.localdate() + timedelta(days=1)
    return timezone.make_aware(datetime.combine(day, time(hour, 0)))


def _png():
    from PIL import Image
    buf = io.BytesIO()
    Image.new('RGB', (2, 2)).save(buf, format='PNG')
    return SimpleUploadedFile('p.png', buf.getvalue(), content_type='image/png')


@override_settings(COD_TRUSTED_MIN_ORDERS=1)
class CashPaymentRulesTests(TestCase):
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
        mk = lambda email, role: User.objects.create_user(
            username=email, email=email, password='Pass1234!', role=role, email_verified=True)
        self.customer = mk('new@e.com', 'customer')
        self.customer.face_verified_at = timezone.now()
        self.customer.id_status = 'verified'
        self.customer.save(update_fields=['face_verified_at', 'id_status'])
        self.owner = mk('o@e.com', 'shop_owner')
        self.rider = mk('r@e.com', 'rider')
        RiderProfile.objects.create(user=self.rider, is_submitted=True, approval_status='approved')
        self.shop = Shop.objects.create(owner=self.owner, name='Suds', address='Cantilan', status='approved',
                                        latitude=9.33, longitude=125.97)
        self.wash = Service.objects.create(shop=self.shop, name='wash_fold', price_per_kg='50.00')
        self.iron = Service.objects.create(shop=self.shop, name='iron', price_per_piece='20.00')

    def _make_trusted(self):
        Order.objects.create(customer=self.customer, shop=self.shop, pickup_address='P', delivery_address='D',
                             pickup_datetime=timezone.now(), payment_method='gcash', payment_status='paid',
                             status='delivered', total_amount='100.00')
        self.customer = User.objects.get(pk=self.customer.pk)   # drop the cached eligibility

    def _book(self, payment, selected=None, **extra):
        data = {'pickup_address': 'P', 'delivery_address': 'D', 'payment_method': payment,
                'pickup_datetime': _tomorrow_at(10).strftime('%Y-%m-%dT%H:%M'),
                'svc_selected': selected if selected is not None else [self.iron.id],
                f'qty_{self.iron.id}': '3'}
        data.update(extra)
        self.client.force_login(self.customer)
        resp = self.client.post(reverse('place_order', args=[self.shop.id]), data)
        return resp, Order.objects.filter(customer=self.customer).order_by('-id').first()

    # ── booking ──
    def test_new_customer_sees_cod_disabled(self):
        self.client.force_login(self.customer)
        page = self.client.get(reverse('place_order', args=[self.shop.id]))
        self.assertContains(page, 'value="cod" disabled')
        self.assertContains(page, 'value="cop" checked')

    def test_new_customer_cannot_book_cod(self):
        resp, order = self._book('cod')
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(order)

    def test_new_customer_can_book_cash_on_pickup(self):
        resp, order = self._book('cop')
        self.assertRedirects(resp, reverse('order_detail', args=[order.id]))
        self.assertEqual(order.payment_method, 'cop')
        self.assertIsNone(order.payment_due_at)
        self.assertFalse(order.awaiting_online_payment)

    def test_cash_on_pickup_rejected_for_weighed_laundry(self):
        resp, order = self._book('cop', selected=[self.wash.id])
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(order)

    def test_trusted_customer_can_book_cod(self):
        self._make_trusted()
        resp, order = self._book('cod')
        self.assertEqual(order.payment_method, 'cod')

    # ── rider collects cash at pickup ──
    def _accepted_cop_order(self):
        _resp, order = self._book('cop')
        Order.objects.filter(pk=order.pk).update(status='accepted', rider=self.rider)
        PhotoProof.objects.create(order=order, photo=_png(), proof_type='pickup', uploaded_by=self.rider)
        return order

    def test_pickup_requires_cash_confirmation(self):
        order = self._accepted_cop_order()
        self.client.force_login(self.rider)
        self.client.post(reverse('rider_update_status', args=[order.id]), {'status': 'picked_up'})
        order.refresh_from_db()
        self.assertEqual(order.status, 'accepted')
        self.client.post(reverse('rider_update_status', args=[order.id]), {'status': 'picked_up', 'cod_collected': 'on'})
        order.refresh_from_db()
        self.assertEqual(order.status, 'picked_up')
        self.assertEqual(order.payment_status, 'paid')

    def test_unpaid_cop_blocks_delivery(self):
        _resp, order = self._book('cop')
        order.status = 'ready'
        self.assertIn('cash-on-pickup', order.delivery_block_reason())

    # ── switching away from an unpaid online payment ──
    def test_new_customer_switches_to_cash_on_pickup_before_pickup(self):
        _resp, order = self._book('gcash')
        self.client.post(reverse('switch_to_cod', args=[order.id]))
        order.refresh_from_db()
        self.assertEqual(order.payment_method, 'cop')

    def test_new_customer_cannot_switch_after_pickup(self):
        _resp, order = self._book('gcash')
        Order.objects.filter(pk=order.pk).update(status='picked_up')
        self.client.post(reverse('switch_to_cod', args=[order.id]))
        order.refresh_from_db()
        self.assertEqual(order.payment_method, 'gcash')

    # ── overdue online payment ──
    @override_settings(PAYMENT_REMINDER_HOURS=[24, 48], PAYMENT_DUE_HOURS=72)
    def test_overdue_new_customer_falls_back_to_cop_then_cod(self):
        _r, before_pickup = self._book('gcash')
        _r, at_shop = self._book('gcash')
        Order.objects.filter(pk=at_shop.pk).update(status='washing')
        Order.objects.filter(pk__in=[before_pickup.pk, at_shop.pk]).update(
            payment_due_at=timezone.now() - timedelta(minutes=1))
        call_command('process_payment_reminders', stdout=io.StringIO())
        before_pickup.refresh_from_db()
        at_shop.refresh_from_db()
        self.assertEqual(before_pickup.payment_method, 'cop')
        self.assertEqual(at_shop.payment_method, 'cod')     # laundry is already at the shop
