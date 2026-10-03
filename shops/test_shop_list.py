from datetime import time
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from reviews.models import Review
from orders.models import Order
from shops.models import Service, Shop

User = get_user_model()


class ShopListPageTests(TestCase):
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
        self.owner = User.objects.create_user(username='o@e.com', email='o@e.com', password='x', role='shop_owner')
        self.cheap = self._shop('Alpha Wash', '30.00')
        self.pricey = self._shop('Bravo Clean', '90.00')
        # Always-open vs never-open hours, so "Open now" is deterministic.
        Shop.objects.filter(id=self.cheap.id).update(operating_hours_start=time(0, 0), operating_hours_end=time(23, 59, 59))
        Shop.objects.filter(id=self.pricey.id).update(operating_hours_start=time(0, 0), operating_hours_end=time(0, 0, 1))
        customer = User.objects.create_user(username='c@e.com', email='c@e.com', password='x', role='customer')
        order = Order.objects.create(customer=customer, shop=self.pricey, pickup_address='P', delivery_address='D',
                                     pickup_datetime='2030-01-01T10:00Z', payment_method='cod', status='delivered')
        Review.objects.create(order=order, customer=customer, shop=self.pricey, rating=5)

    def _shop(self, name, price):
        shop = Shop.objects.create(owner=self.owner, name=name, address='Tandag', status='approved')
        Service.objects.create(shop=shop, name='wash', price_per_kg=price)
        return shop

    def _names(self, **params):
        return [s.name for s in self.client.get(reverse('shop_list'), params).context['shops']]

    def test_sorts(self):
        self.assertEqual(self._names(sort='price'), ['Alpha Wash', 'Bravo Clean'])
        self.assertEqual(self._names(sort='rated'), ['Bravo Clean', 'Alpha Wash'])
        self.assertEqual(self._names(sort='name'), ['Alpha Wash', 'Bravo Clean'])
        self.assertEqual(len(self._names(sort='bogus')), 2)          # invalid sort falls back

    def test_open_now_filter(self):
        self.assertEqual(self._names(open='1'), ['Alpha Wash'])

    def test_service_chips_keep_other_filters(self):
        resp = self.client.get(reverse('shop_list'), {'q': 'Tandag', 'sort': 'price'})
        chips = {value: qs for value, _label, qs in resp.context['service_chips']}
        self.assertIn('q=Tandag', chips['iron'])
        self.assertIn('sort=price', chips['iron'])
        self.assertIn('service=iron', chips['iron'])

    def test_pagination(self):
        for i in range(13):
            self._shop(f'Shop {i:02d}', '50.00')
        first = self.client.get(reverse('shop_list'))
        self.assertEqual((len(first.context['shops']), first.context['result_count']), (12, 15))
        self.assertContains(first, 'page=2')
        self.assertEqual(len(self.client.get(reverse('shop_list'), {'page': 2}).context['shops']), 3)
        self.assertEqual(self.client.get(reverse('shop_list'), {'page': 'abc'}).status_code, 200)

    def test_empty_state_offers_clear(self):
        resp = self.client.get(reverse('shop_list'), {'q': 'nothing-here'})
        self.assertContains(resp, 'No shops match your filters')
        self.assertContains(resp, 'Clear filters')
