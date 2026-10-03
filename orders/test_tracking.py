from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from maps.models import RiderLocation
from orders.models import Order
from shops.models import Shop

User = get_user_model()


class RiderTrackingTests(TestCase):
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
            username=email, email=email, password='Pass1234!', role=role, email_verified=True
        )
        self.customer = mk('c@example.com', 'customer')
        self.rider = mk('r@example.com', 'rider')
        owner = mk('o@example.com', 'shop_owner')
        shop = Shop.objects.create(owner=owner, name='S', address='A', status='approved')
        self.order = Order.objects.create(
            customer=self.customer, shop=shop, rider=self.rider, pickup_address='P', delivery_address='D',
            delivery_lat=9.3, delivery_lng=125.9, pickup_datetime=timezone.now() + timedelta(days=1),
            payment_method='cod', total_amount='100.00', status='out_for_delivery',
        )
        RiderLocation.objects.create(rider=self.rider, latitude=9.33, longitude=125.97)
        self.client.force_login(self.customer)

    def test_status_api_includes_location_and_timestamp(self):
        data = self.client.get(reverse('api_order_status', args=[self.order.id])).json()
        self.assertEqual((data['rider_lat'], data['rider_lng']), (9.33, 125.97))
        self.assertIn('rider_updated_at', data)

    def test_order_page_renders_initial_rider_position(self):
        resp = self.client.get(reverse('order_detail', args=[self.order.id]))
        self.assertContains(resp, 'data-rider-lat="9.33"')
        self.assertContains(resp, 'data-rider-updated-at=')
        self.assertContains(resp, 'id="trackingStatus"')

    def test_no_coordinates_outside_out_for_delivery(self):
        for status in ('accepted', 'ready', 'delivered'):
            self.order.status = status
            self.order.save(update_fields=['status'])
            page = self.client.get(reverse('order_detail', args=[self.order.id]))
            self.assertNotContains(page, 'data-rider-lat', msg_prefix=status)
            self.assertIsNone(page.context['rider_location'], status)
            data = self.client.get(reverse('api_order_status', args=[self.order.id])).json()
            self.assertNotIn('rider_lat', data, status)
            self.assertNotIn('rider_updated_at', data, status)

    def test_other_customer_cannot_poll(self):
        other = User.objects.create_user(username='x@example.com', email='x@example.com', password='x', role='customer')
        self.client.force_login(other)
        self.assertEqual(self.client.get(reverse('api_order_status', args=[self.order.id])).status_code, 404)
