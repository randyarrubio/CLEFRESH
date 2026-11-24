"""Loyalty points: earn on completed orders, redeem at booking, restore on cancel, revoke on refund."""
import io
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import RiderProfile
from orders import loyalty
from orders.models import LoyaltyEntry, Order, PhotoProof
from shops.models import Service, Shop

User = get_user_model()


def _png():
    from PIL import Image
    buf = io.BytesIO()
    Image.new('RGB', (2, 2)).save(buf, format='PNG')
    return SimpleUploadedFile('p.png', buf.getvalue(), content_type='image/png')


@override_settings(LOYALTY_PESOS_PER_POINT=20, LOYALTY_MAX_REDEEM_PERCENT=50, COD_TRUSTED_MIN_ORDERS=0)
class LoyaltyTests(TestCase):
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
        self.customer = mk('c@e.com', 'customer')
        self.customer.face_verified_at = timezone.now()
        self.customer.id_status = 'verified'
        self.customer.save()
        self.owner = mk('o@e.com', 'shop_owner')
        self.rider = mk('r@e.com', 'rider')
        RiderProfile.objects.create(user=self.rider, is_submitted=True, approval_status='approved')
        self.shop = Shop.objects.create(owner=self.owner, name='Suds', address='Cantilan', status='approved',
                                        latitude=9.3, longitude=125.9)
        self.iron = Service.objects.create(shop=self.shop, name='iron', price_per_piece='50.00')

    def _book(self, pieces=4, use_points=False, payment='cod'):
        self.client.force_login(self.customer)
        data = {'pickup_address': 'P', 'delivery_address': 'D', 'payment_method': payment,
                'pickup_datetime': (timezone.localdate() + timedelta(days=1)).strftime('%Y-%m-%dT10:00'),
                'svc_selected': [self.iron.id], f'qty_{self.iron.id}': str(pieces)}
        if use_points:
            data['use_points'] = '1'
        self.client.post(reverse('place_order', args=[self.shop.id]), data)
        return Order.objects.filter(customer=self.customer).order_by('-id').first()

    def _give(self, points):
        LoyaltyEntry.objects.create(user=self.customer, kind='earn', points=points)

    def test_cod_delivery_by_rider_awards_points_once(self):
        order = self._book(pieces=5)                         # ₱250 -> 12 points
        Order.objects.filter(pk=order.pk).update(status='out_for_delivery', rider=self.rider)
        PhotoProof.objects.create(order=order, photo=_png(), proof_type='delivery', uploaded_by=self.rider)
        self.client.force_login(self.rider)
        self.client.post(reverse('rider_update_status', args=[order.id]), {'status': 'delivered', 'cod_collected': 'on'})
        self.assertEqual(loyalty.balance(self.customer), 12)
        loyalty.award(order.id)                              # repeat call is a no-op
        self.assertEqual(loyalty.balance(self.customer), 12)

    def test_unpaid_delivery_earns_nothing_until_paid(self):
        order = self._book()
        Order.objects.filter(pk=order.pk).update(status='delivered')
        self.assertEqual(loyalty.award(order.id), 0)

    def test_redeem_caps_at_half_the_order(self):
        self._give(500)
        order = self._book(pieces=4, use_points=True)        # ₱200 -> max 100 points
        self.assertEqual(order.points_redeemed, 100)
        self.assertEqual(order.total_amount, Decimal('100.00'))
        self.assertEqual(loyalty.balance(self.customer), 400)

    def test_redeem_keeps_minimum_charge(self):
        self._give(100)
        order = self._book(pieces=1, use_points=True)        # ₱50: 50% = 25, min charge leaves 30 -> 25
        self.assertEqual(order.points_redeemed, 25)
        self._give(0)
        self.assertEqual(loyalty.max_redeemable(100, Decimal('30')), 10)   # ₱30 can only drop to ₱20

    def test_points_not_used_unless_ticked(self):
        self._give(50)
        order = self._book()
        self.assertEqual(order.points_redeemed, 0)
        self.assertEqual(loyalty.balance(self.customer), 50)

    def test_cancel_restores_spent_points(self):
        self._give(60)
        order = self._book(pieces=4, use_points=True)
        self.assertEqual(loyalty.balance(self.customer), 0)
        self.client.post(reverse('cancel_order', args=[order.id]))
        self.assertEqual(loyalty.balance(self.customer), 60)

    def test_refund_revokes_earned_points(self):
        order = self._book(pieces=5)
        Order.objects.filter(pk=order.pk).update(status='delivered', payment_status='paid')
        loyalty.award(order.id)
        self.assertEqual(loyalty.balance(self.customer), 12)
        loyalty.revoke(Order.objects.get(pk=order.pk))
        self.assertEqual(loyalty.balance(self.customer), 0)

    def test_balance_shown_on_my_orders(self):
        self._give(30)
        self.client.force_login(self.customer)
        self.assertContains(self.client.get(reverse('order_list')), '30 loyalty points')
