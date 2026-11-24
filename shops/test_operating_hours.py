"""Pick-ups and delivery slots must fall inside the shop's operating hours."""
from datetime import time, timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from orders.models import Order
from shops.models import DeliverySlot, Service, Shop

User = get_user_model()


class OperatingHoursTests(TestCase):
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
        self.customer.save(update_fields=['face_verified_at', 'id_status'])
        self.owner = mk('o@e.com', 'shop_owner')
        self.shop = Shop.objects.create(owner=self.owner, name='Suds', address='Cantilan', status='approved',
                                        latitude=9.33, longitude=125.97,
                                        operating_hours_start=time(8, 0), operating_hours_end=time(17, 0))
        self.iron = Service.objects.create(shop=self.shop, name='iron', price_per_piece='20.00')
        self.tomorrow = timezone.localdate() + timedelta(days=1)

    def _book(self, hhmm, **extra):
        self.client.force_login(self.customer)
        data = {'pickup_address': 'P', 'delivery_address': 'D', 'payment_method': 'cop',
                'pickup_datetime': f'{self.tomorrow:%Y-%m-%d}T{hhmm}',
                'svc_selected': [self.iron.id], f'qty_{self.iron.id}': '2'}
        data.update(extra)
        return self.client.post(reverse('place_order', args=[self.shop.id]), data)

    def test_within_hours(self):
        self.assertTrue(self.shop.within_hours(time(8, 0)))
        self.assertTrue(self.shop.within_hours(time(17, 0)))
        self.assertFalse(self.shop.within_hours(time(7, 59)))
        self.assertFalse(self.shop.within_hours(time(9, 0), time(18, 0)))
        self.assertEqual(self.shop.hours_label, '8:00 AM – 5:00 PM')

    def test_pickup_outside_hours_rejected(self):
        resp = self._book('06:30')
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'operating hours')
        self.assertFalse(Order.objects.exists())

    def test_pickup_inside_hours_accepted(self):
        self._book('09:30')
        self.assertTrue(Order.objects.exists())

    def test_slot_outside_hours_is_hidden_and_rejected(self):
        late = DeliverySlot.objects.create(shop=self.shop, date=self.tomorrow, time_start=time(18, 0), time_end=time(20, 0))
        self.client.force_login(self.customer)
        page = self.client.get(reverse('place_order', args=[self.shop.id]))
        self.assertNotContains(page, f'<option value="{late.id}">')
        api = self.client.get(reverse('api_slots', args=[self.shop.id])).json()
        self.assertEqual(api['slots'], [])
        self._book('09:30', delivery_slot=late.id)
        self.assertFalse(Order.objects.exists())

    def test_slot_before_pickup_date_rejected(self):
        today_slot = DeliverySlot.objects.create(shop=self.shop, date=timezone.localdate(),
                                                 time_start=time(9, 0), time_end=time(10, 0))
        self._book('09:30', delivery_slot=today_slot.id)
        self.assertFalse(Order.objects.exists())

    def test_owner_cannot_create_slot_outside_hours(self):
        self.client.force_login(self.owner)
        self.client.post(reverse('create_slot'), {'date': f'{self.tomorrow}', 'time_start': '16:00',
                                                  'time_end': '19:00', 'max_capacity': 3})
        self.assertFalse(DeliverySlot.objects.exists())
        self.client.post(reverse('create_slot'), {'date': f'{self.tomorrow}', 'time_start': '09:00',
                                                  'time_end': '11:00', 'max_capacity': 3})
        self.assertTrue(DeliverySlot.objects.exists())
