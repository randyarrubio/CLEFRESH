"""Shop attendants (accounts, permissions, time-in/out, earnings) and the walk-in POS."""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from orders.models import LoyaltyEntry, Order
from shops.models import AttendanceRecord, Service, Shop, ShopAttendant

User = get_user_model()


class _Base(TestCase):
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
        self.owner = User.objects.create_user(username='o@e.com', email='o@e.com', password='Pass1234!',
                                              role='shop_owner', email_verified=True)
        self.shop = Shop.objects.create(owner=self.owner, name='Suds', address='Cantilan', status='approved',
                                        latitude=9.3, longitude=125.9)
        self.wash = Service.objects.create(shop=self.shop, name='wash_fold', price_per_kg='40.00')
        self.iron = Service.objects.create(shop=self.shop, name='iron', price_per_piece='15.00')

    def _attendant(self, rate='60.00', **kw):
        user = User.objects.create_user(username='a@e.com', email='a@e.com', password='Pass1234!',
                                        role='attendant', email_verified=True)
        return ShopAttendant.objects.create(shop=self.shop, user=user, hourly_rate=rate, **kw)


class AttendantTests(_Base):
    def test_owner_creates_attendant_account(self):
        self.client.force_login(self.owner)
        self.client.post(reverse('create_attendant'), {
            'first_name': 'Ana', 'last_name': 'Cruz', 'email': 'ana@e.com', 'password': 'Counter#2026',
            'hourly_rate': '62.5', 'shift_start': '08:00', 'shift_end': '17:00', 'work_days': ['mon', 'tue']})
        profile = ShopAttendant.objects.get()
        self.assertEqual(profile.user.role, 'attendant')
        self.assertEqual(profile.hourly_rate, Decimal('62.50'))
        self.assertEqual(profile.work_days, ['mon', 'tue'])
        self.assertTrue(profile.user.check_password('Counter#2026'))

    def test_attendant_can_work_orders_but_not_owner_pages(self):
        att = self._attendant()
        self.client.force_login(att.user)
        self.assertEqual(self.client.get(reverse('shop_dashboard')).status_code, 200)
        self.assertEqual(self.client.get(reverse('shop_dashboard_orders')).status_code, 200)
        self.assertEqual(self.client.get(reverse('shop_pos')).status_code, 200)
        for name in ('shop_dashboard_services', 'shop_dashboard_payments', 'shop_staff', 'shop_dashboard_promos'):
            self.assertEqual(self.client.get(reverse(name)).status_code, 302, name)

    def test_attendant_updates_order_status_with_their_name_logged(self):
        att = self._attendant()
        customer = User.objects.create_user(username='c', email='c@e.com', password='x', role='customer')
        order = Order.objects.create(customer=customer, shop=self.shop, pickup_address='P', delivery_address='D',
                                     pickup_datetime=timezone.now(), payment_method='gcash')
        self.client.force_login(att.user)
        self.client.post(reverse('update_order_status', args=[order.id]), {'status': 'accepted'})
        order.refresh_from_db()
        self.assertEqual(order.status, 'accepted')
        self.assertIn('a@e.com', order.status_logs.last().note)

    def test_time_in_out_and_earnings(self):
        att = self._attendant(rate='60.00')
        self.client.force_login(att.user)
        self.client.post(reverse('attendant_clock'), {'action': 'in'})
        self.client.post(reverse('attendant_clock'), {'action': 'in'})       # double time-in is ignored
        self.assertEqual(AttendanceRecord.objects.count(), 1)
        rec = AttendanceRecord.objects.get()
        AttendanceRecord.objects.filter(pk=rec.pk).update(time_in=timezone.now() - timedelta(hours=2, minutes=30))
        self.client.post(reverse('attendant_clock'), {'action': 'out'})
        rec.refresh_from_db()
        self.assertIsNotNone(rec.time_out)
        self.assertEqual(rec.hours, Decimal('2.50'))
        self.assertEqual(rec.earnings, Decimal('150.00'))
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(reverse('shop_staff')), '₱150.00')

    def test_late_time_in_flagged(self):
        from datetime import time
        att = self._attendant(shift_start=time(8, 0))
        local_day = timezone.localdate()
        late = timezone.make_aware(timezone.datetime.combine(local_day, time(8, 40)))
        ok = timezone.make_aware(timezone.datetime.combine(local_day, time(8, 10)))
        self.assertTrue(AttendanceRecord(attendant=att, time_in=late).is_late)
        self.assertFalse(AttendanceRecord(attendant=att, time_in=ok).is_late)

    def test_deactivated_attendant_is_locked_out(self):
        att = self._attendant()
        AttendanceRecord.objects.create(attendant=att, time_in=timezone.now(), hourly_rate=att.hourly_rate)
        self.client.force_login(self.owner)
        self.client.post(reverse('update_attendant', args=[att.id]), {'action': 'toggle'})
        att.refresh_from_db()
        att.user.refresh_from_db()
        self.assertFalse(att.is_active)
        self.assertFalse(att.user.is_active)
        self.assertIsNotNone(AttendanceRecord.objects.get().time_out)   # open shift closed


class PosTests(_Base):
    def _ring_up(self, user=None, **extra):
        self.client.force_login(user or self.owner)
        data = {'walk_in_name': 'Maria', 'walk_in_phone': '0917', 'payment': 'counter_paid',
                f'weight_{self.wash.id}': '3.5', f'qty_{self.iron.id}': '2'}
        data.update(extra)
        return self.client.post(reverse('shop_pos'), data)

    def test_walk_in_order_priced_and_paid_at_counter(self):
        resp = self._ring_up()
        order = Order.objects.get()
        self.assertRedirects(resp, reverse('pos_receipt', args=[order.id]))
        self.assertTrue(order.is_walk_in)
        self.assertIsNone(order.customer)
        self.assertEqual(order.total_amount, Decimal('170.00'))     # 3.5 kg x 40 + 2 x 15
        self.assertEqual(order.payment_status, 'paid')
        self.assertEqual(order.payment_method, 'counter')
        self.assertEqual(order.commission_rate, Decimal('0'))
        receipt = self.client.get(reverse('pos_receipt', args=[order.id]))
        self.assertContains(receipt, 'Maria')
        self.assertContains(receipt, 'PAID')

    def test_attendant_can_ring_up_walk_in(self):
        att = self._attendant()
        self._ring_up(user=att.user)
        self.assertTrue(Order.objects.filter(is_walk_in=True).exists())

    def test_nothing_entered_is_rejected(self):
        self._ring_up(**{f'weight_{self.wash.id}': '', f'qty_{self.iron.id}': '0'})
        self.assertFalse(Order.objects.exists())

    def test_walk_in_skips_rider_and_is_claimed_with_cash(self):
        self._ring_up(payment='counter_unpaid')
        order = Order.objects.get()
        self.client.post(reverse('update_order_status', args=[order.id]), {'status': 'ready'})
        order.refresh_from_db()
        self.assertEqual(order.status, 'ready')
        self.client.post(reverse('update_order_status', args=[order.id]), {'status': 'out_for_delivery'})
        order.refresh_from_db()
        self.assertEqual(order.status, 'ready')                     # no delivery leg for walk-ins
        self.client.post(reverse('pos_claim', args=[order.id]))      # cash not confirmed
        order.refresh_from_db()
        self.assertEqual(order.status, 'ready')
        self.client.post(reverse('pos_claim', args=[order.id]), {'cash_collected': 'on'})
        order.refresh_from_db()
        self.assertEqual(order.status, 'delivered')
        self.assertEqual(order.payment_status, 'paid')

    def test_linked_customer_earns_points_on_claim(self):
        customer = User.objects.create_user(username='c', email='c@e.com', password='x', role='customer')
        self._ring_up(customer_email='c@e.com')
        order = Order.objects.get()
        self.assertEqual(order.customer, customer)
        Order.objects.filter(pk=order.pk).update(status='ready')
        self.client.post(reverse('pos_claim', args=[order.id]))
        self.assertTrue(LoyaltyEntry.objects.filter(user=customer, kind='earn').exists())

    def test_unknown_customer_email_rejected(self):
        self._ring_up(customer_email='nobody@e.com')
        self.assertFalse(Order.objects.exists())
