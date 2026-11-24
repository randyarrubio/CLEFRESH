"""Admin commission (rate snapshot per order) and the shop/admin payment dashboards."""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from orders.models import Order
from payments.models import PlatformSettings, Refund
from shops.models import Service, Shop
from utils import finance

User = get_user_model()


class FinanceTests(TestCase):
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
        self.admin = mk('a@e.com', 'admin')
        self.owner = mk('o@e.com', 'shop_owner')
        self.customer = mk('c@e.com', 'customer')
        self.shop = Shop.objects.create(owner=self.owner, name='Suds', address='Cantilan', status='approved',
                                        latitude=9.3, longitude=125.9)

    def _order(self, total, method='gcash', status='delivered', pay='paid', rate='10.00'):
        return Order.objects.create(customer=self.customer, shop=self.shop, pickup_address='P', delivery_address='D',
                                    pickup_datetime=timezone.now(), payment_method=method, payment_status=pay,
                                    status=status, total_amount=total, commission_rate=rate)

    def test_commission_amount_and_shop_share(self):
        order = self._order('250.00', rate='12.50')
        self.assertEqual(order.commission_amount, Decimal('31.25'))
        self.assertEqual(order.shop_earnings, Decimal('218.75'))
        legacy = self._order('100.00', rate=None)
        self.assertEqual(legacy.commission_amount, Decimal('0.00'))

    def test_new_orders_snapshot_current_rate(self):
        PlatformSettings.objects.update_or_create(pk=1, defaults={'commission_percent': Decimal('7.50')})
        self.customer.face_verified_at = timezone.now()
        self.customer.id_status = 'verified'
        self.customer.save()
        iron = Service.objects.create(shop=self.shop, name='iron', price_per_piece='20.00')
        self.client.force_login(self.customer)
        day = (timezone.localdate() + timedelta(days=1)).strftime('%Y-%m-%dT10:00')
        self.client.post(reverse('place_order', args=[self.shop.id]), {
            'pickup_address': 'P', 'delivery_address': 'D', 'payment_method': 'cop', 'pickup_datetime': day,
            'svc_selected': [iron.id], f'qty_{iron.id}': '5'})
        self.assertEqual(Order.objects.get().commission_rate, Decimal('7.50'))

    def test_summary_counts_paid_cash_online_outstanding_and_refunds(self):
        self._order('100.00', method='gcash')
        self._order('200.00', method='cod')
        self._order('50.00', status='washing', pay='unpaid')
        refunded = self._order('80.00', pay='refunded')
        Refund.objects.create(order=refunded, amount=8000, reason='others', status='succeeded')
        s = finance.summarize(Order.objects.filter(shop=self.shop))
        self.assertEqual(s['gross'], Decimal('300.00'))
        self.assertEqual(s['commission'], Decimal('30.00'))
        self.assertEqual(s['net'], Decimal('270.00'))
        self.assertEqual(s['cash'], Decimal('200.00'))
        self.assertEqual(s['online'], Decimal('100.00'))
        self.assertEqual(s['outstanding'], Decimal('50.00'))
        self.assertEqual(s['refunds'], Decimal('80.00'))

    def test_shop_payments_page_shows_earnings(self):
        self._order('100.00')
        self.client.force_login(self.owner)
        page = self.client.get(reverse('shop_dashboard_payments') + '?days=7')
        self.assertContains(page, '₱90.00')           # net after 10%
        self.assertContains(page, 'Your earnings after commission')

    def test_admin_sets_rate_and_sees_per_shop_totals(self):
        self._order('100.00')
        self.client.force_login(self.admin)
        self.client.post(reverse('admin_finance'), {'commission_percent': '15'})
        self.assertEqual(PlatformSettings.current().commission_percent, Decimal('15.00'))
        page = self.client.get(reverse('admin_finance'))
        self.assertContains(page, 'Suds')
        self.assertContains(page, '₱10.00')           # commission on the existing 10% order
        self.client.post(reverse('admin_finance'), {'commission_percent': '80'})
        self.assertEqual(PlatformSettings.current().commission_percent, Decimal('15.00'))

    def test_finance_page_is_admin_only(self):
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(reverse('admin_finance')).status_code, 302)
