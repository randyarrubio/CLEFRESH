"""Login limits, client IP, check-email limit, pagination, rider map/location gating."""
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import RiderProfile
from orders.models import Order
from shops.models import Shop

User = get_user_model()


class Round2Tests(TestCase):
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
        cache.clear()
        mk = lambda email, role: User.objects.create_user(
            username=email, email=email, password='Pass1234!', role=role, email_verified=True)
        self.customer = mk('c@e.com', 'customer')
        self.admin = mk('a@e.com', 'admin')
        self.owner = mk('o@e.com', 'shop_owner')
        self.rider = mk('r@e.com', 'rider')
        RiderProfile.objects.create(user=self.rider, is_submitted=True, approval_status='approved')
        self.shop = Shop.objects.create(owner=self.owner, name='Suds', address='Tandag', status='approved')

    def _login(self, password, ip):
        return self.client.post(reverse('login'), {'username': 'c@e.com', 'password': password}, REMOTE_ADDR=ip)

    def _logged_in(self):
        return '_auth_user_id' in self.client.session

    def test_stranger_cannot_lock_out_the_owner(self):
        for _ in range(10):
            self._login('Wrong1234!', '10.0.0.66')            # attacker hammers the account
        self._login('Pass1234!', '10.0.0.1')              # owner, from their own network
        self.assertTrue(self._logged_in())

    def test_same_ip_is_locked_after_ten_failures(self):
        for _ in range(10):
            self._login('Wrong1234!', '10.0.0.66')
        self._login('Pass1234!', '10.0.0.66')
        self.assertFalse(self._logged_in())

    def test_spread_out_guessing_is_capped_per_account(self):
        for i in range(50):
            self._login('Wrong1234!', f'10.1.{i}.1')
        self._login('Pass1234!', '10.0.0.1')
        self.assertFalse(self._logged_in())

    @override_settings(CLIENT_IP_HEADER='HTTP_X_REAL_IP')
    def test_proxy_header_identifies_each_visitor(self):
        for _ in range(10):
            self.client.post(reverse('login'), {'username': 'x@e.com', 'password': 'Wrong1234!'},
                             REMOTE_ADDR='127.0.0.1', HTTP_X_REAL_IP='203.0.113.9')
        # Another visitor behind the same proxy isn't blocked.
        self.client.post(reverse('login'), {'username': 'c@e.com', 'password': 'Pass1234!'},
                         REMOTE_ADDR='127.0.0.1', HTTP_X_REAL_IP='198.51.100.7')
        self.assertTrue(self._logged_in())

    def test_check_email_is_tightly_limited(self):
        codes = [self.client.get(reverse('check_email'), {'email': f'u{i}@e.com'}).status_code for i in range(11)]
        self.assertEqual(codes[:10], [200] * 10)
        self.assertEqual(codes[10], 429)

    def test_admin_lists_are_paginated(self):
        for i in range(30):
            User.objects.create_user(username=f'u{i}@e.com', email=f'u{i}@e.com', password='x', role='customer')
        self.client.force_login(self.admin)
        resp = self.client.get(reverse('admin_users'), {'role': 'customer'})
        self.assertEqual(len(resp.context['users']), 25)
        self.assertContains(resp, 'page=2')
        self.assertContains(resp, 'role=customer&amp;page=2')    # filter kept in page links
        self.assertEqual(self.client.get(reverse('admin_users'), {'page': 'zzz'}).status_code, 200)

    def test_rider_shares_location_only_while_delivering(self):
        order = Order.objects.create(customer=self.customer, shop=self.shop, rider=self.rider,
                                     pickup_address='12 Rizal St', delivery_address='5 Luna St',
                                     pickup_datetime=timezone.now() + timedelta(days=1), status='accepted')
        self.client.force_login(self.rider)
        url = reverse('rider_task_detail', args=[order.id])
        resp = self.client.get(url)
        self.assertContains(resp, 'const isRiderPage = false')
        self.assertContains(resp, 'destination=12%20Rizal%20St')        # navigate to pickup
        Order.objects.filter(pk=order.pk).update(status='out_for_delivery')
        resp = self.client.get(url)
        self.assertContains(resp, 'const isRiderPage = true')
        self.assertContains(resp, 'destination=5%20Luna%20St')          # navigate to customer
        self.assertTrue(self.client.get(reverse('rider_dashboard')).context['has_active_delivery'])
