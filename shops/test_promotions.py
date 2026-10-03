from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from notifications.models import Notification
from shops.models import Promotion, Service, Shop

User = get_user_model()


class PromotionTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Email helper writes EmailLog from a thread; keep it out of the test DB.
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
        self.owner = mk('o@example.com', 'shop_owner')
        self.other_owner = mk('o2@example.com', 'shop_owner')
        self.customer = mk('c@example.com', 'customer')
        self.shop = Shop.objects.create(owner=self.owner, name='Suds', address='Tandag', status='approved')
        self.other_shop = Shop.objects.create(owner=self.other_owner, name='Other', address='Tandag', status='approved')
        Service.objects.create(shop=self.shop, name='wash', price_per_kg='40.00')
        self.today = timezone.localdate()

    def _promo(self, shop=None, **kw):
        data = dict(shop=shop or self.shop, title='Rainy day sale', discount_type='percent', discount_value='20',
                    start_date=self.today, end_date=self.today + timedelta(days=5))
        data.update(kw)
        return Promotion.objects.create(**data)

    def _post(self, **data):
        base = {'title': 'Weekend 15%', 'description': 'All wash services', 'discount_type': 'percent',
                'discount_value': '15', 'start_date': self.today.isoformat(),
                'end_date': (self.today + timedelta(days=3)).isoformat(), 'is_active': 'on'}
        base.update(data)
        self.client.force_login(self.owner)
        return self.client.post(reverse('shop_dashboard_promos'), base)

    # ── owner CRUD ──
    def test_owner_posts_promo(self):
        resp = self._post()
        self.assertRedirects(resp, reverse('shop_dashboard_promos'))
        promo = Promotion.objects.get(shop=self.shop)
        self.assertEqual((promo.title, promo.status, promo.discount_label), ('Weekend 15%', 'live', '15% off'))

    def test_validation_rules(self):
        self._post(discount_value='150')
        self._post(discount_type='fixed', discount_value='')
        self._post(end_date=(self.today - timedelta(days=1)).isoformat())
        self._post(title='   ')
        self.assertFalse(Promotion.objects.exists())

    def test_special_offer_needs_no_value(self):
        self._post(discount_type='offer', discount_value='99', title='Free fold')
        promo = Promotion.objects.get()
        self.assertIsNone(promo.discount_value)
        self.assertEqual(promo.discount_label, 'Special offer')

    def test_owner_edits_pauses_and_deletes(self):
        promo = self._promo()
        self._post(promo_id=promo.id, title='Updated title')
        promo.refresh_from_db()
        self.assertEqual(promo.title, 'Updated title')
        self.client.post(reverse('toggle_promo', args=[promo.id]))
        promo.refresh_from_db()
        self.assertEqual(promo.status, 'paused')
        self.client.post(reverse('delete_promo', args=[promo.id]))
        self.assertFalse(Promotion.objects.exists())

    def test_owner_cannot_touch_other_shops_promos(self):
        theirs = self._promo(shop=self.other_shop)
        self.client.force_login(self.owner)
        self.assertEqual(self.client.get(reverse('shop_dashboard_promos'), {'edit': theirs.id}).status_code, 404)
        self.assertEqual(self.client.post(reverse('toggle_promo', args=[theirs.id])).status_code, 404)
        self.assertEqual(self.client.post(reverse('delete_promo', args=[theirs.id])).status_code, 404)
        self._post(promo_id=theirs.id, title='hijacked')
        theirs.refresh_from_db()
        self.assertEqual(theirs.title, 'Rainy day sale')

    def test_customer_cannot_manage_promos(self):
        self.client.force_login(self.customer)
        self.assertEqual(self.client.get(reverse('shop_dashboard_promos')).status_code, 302)

    def test_delete_requires_post(self):
        promo = self._promo()
        self.client.force_login(self.owner)
        self.client.get(reverse('delete_promo', args=[promo.id]))
        self.assertTrue(Promotion.objects.filter(id=promo.id).exists())

    # ── what counts as live ──
    def test_live_excludes_paused_scheduled_and_expired(self):
        live = self._promo(title='live')
        self._promo(title='paused', is_active=False)
        self._promo(title='scheduled', start_date=self.today + timedelta(days=2), end_date=self.today + timedelta(days=4))
        self._promo(title='expired', start_date=self.today - timedelta(days=5), end_date=self.today - timedelta(days=1))
        self.assertEqual(list(Promotion.objects.live()), [live])

    # ── customer visibility ──
    def test_live_promo_visible_on_customer_pages(self):
        self._promo(title='Rainy day sale')
        self._promo(title='Hidden paused promo', is_active=False)
        self.client.force_login(self.customer)
        pages = [reverse('shop_detail', args=[self.shop.id]), reverse('shop_site_services', args=[self.shop.id]),
                 reverse('place_order', args=[self.shop.id])]
        for url in pages:
            resp = self.client.get(url)
            self.assertContains(resp, 'Rainy day sale', msg_prefix=url)
            self.assertNotContains(resp, 'Hidden paused promo', msg_prefix=url)
        home = self.client.get(reverse('home'))
        self.assertContains(home, 'promo-badge')
        self.assertContains(home, '20% off')

    def test_badge_on_public_shop_list(self):
        self._promo(discount_type='fixed', discount_value='50')
        self.assertContains(self.client.get(reverse('shop_list')), '₱50.00 off')

    def test_promo_text_is_escaped(self):
        self._promo(title='<script>alert(1)</script>')
        resp = self.client.get(reverse('shop_detail', args=[self.shop.id]))
        self.assertNotContains(resp, '<script>alert(1)</script>')
        self.assertContains(resp, '&lt;script&gt;alert(1)&lt;/script&gt;')

    # ── automatic discount at checkout ──
    def _book(self, weight='5', **extra):
        """Book wash (₱40/kg), then — as the shop does at drop-off — weigh it at `weight` kg.

        Weighing is shop-only, so promos are applied when the shop confirms the weight.
        """
        from orders.models import Order
        svc = self.shop.services.get(name='wash')
        data = {'pickup_address': 'P', 'delivery_address': 'D', 'payment_method': 'cod',
                'pickup_datetime': (timezone.localtime() + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M'),
                'svc_selected': [svc.id]}
        data.update(extra)
        self.client.force_login(self.customer)
        self.client.post(reverse('place_order', args=[self.shop.id]), data)
        order = Order.objects.filter(customer=self.customer).latest('id')
        Order.objects.filter(id=order.id).update(status='picked_up')      # laundry reached the shop
        line = order.order_services.get(service=svc)
        self.client.force_login(self.owner)
        self.client.post(reverse('confirm_order_weight', args=[order.id]), {f'weight_{line.id}': weight})
        self.client.force_login(self.customer)
        order.refresh_from_db()
        return order

    def test_percent_promo_deducted_and_recorded(self):
        promo = self._promo(discount_value='20')           # 20% of ₱200
        order = self._book('5')
        self.assertEqual((str(order.subtotal_amount), str(order.discount_amount), str(order.total_amount)),
                         ('200.00', '40.00', '160.00'))
        self.assertEqual((order.promotion, order.promotion_title), (promo, 'Rainy day sale'))
        self.assertIn('₱160.00', Notification.objects.filter(user=self.customer).latest('id').message)

    def test_best_promo_wins(self):
        self._promo(title='10%', discount_value='10')        # ₱20 on ₱200
        best = self._promo(title='Flat 50', discount_type='fixed', discount_value='50')
        self._promo(title='Offer', discount_type='offer', discount_value=None)
        order = self._book('5')
        self.assertEqual((order.promotion, str(order.total_amount)), (best, '150.00'))

    def test_minimum_order_respected(self):
        self._promo(discount_value='20', min_order_amount='500')
        order = self._book('5')                               # ₱200 < ₱500
        self.assertEqual((order.promotion, str(order.discount_amount), str(order.total_amount)), (None, '0.00', '200.00'))

    def test_discount_never_zeroes_order(self):
        self._promo(discount_type='fixed', discount_value='40')
        order = self._book('1')                               # ₱40 subtotal, ₱40 off -> not applied
        self.assertEqual(str(order.total_amount), '40.00')
        self.assertIsNone(order.promotion)

    def test_inactive_promos_not_applied(self):
        self._promo(is_active=False)
        self._promo(start_date=self.today + timedelta(days=1), end_date=self.today + timedelta(days=3))
        self._promo(start_date=self.today - timedelta(days=5), end_date=self.today - timedelta(days=1))
        self.assertIsNone(self._book('5').promotion)

    def test_percent_capped_at_90(self):
        self._post(discount_value='95')
        self.assertFalse(Promotion.objects.exists())

    def test_paymongo_charged_discounted_total_in_centavos(self):
        from django.test import override_settings
        self._promo(discount_value='20')
        order = self._book('5', payment_method='gcash')      # ₱160 after discount
        captured = {}

        def fake_post(url, **kw):
            captured.setdefault('amount', kw['json']['data']['attributes'].get('amount'))
            raise RuntimeError('stop before network')

        with override_settings(PAYMONGO_SECRET_KEY='sk_test'), \
                mock.patch('payments.views.requests.post', side_effect=fake_post):
            self.client.post(reverse('payment_process', args=[order.id]), {'payment_method_type': 'gcash'})
        self.assertEqual(captured['amount'], 16000)

    def test_breakdown_shown_on_order_pages(self):
        self._promo(discount_value='20')
        order = self._book('5')
        resp = self.client.get(reverse('order_detail', args=[order.id]))
        self.assertContains(resp, '−₱40.00')
        self.assertContains(resp, '₱200.00')
        self.client.force_login(self.owner)
        self.assertContains(self.client.get(reverse('shop_dashboard_order_detail', args=[order.id])), '−₱40.00')

    def test_booking_page_gets_estimate_rules(self):
        self._promo(discount_value='20', min_order_amount='100')
        self._promo(title='Offer only', discount_type='offer', discount_value=None)
        self.client.force_login(self.customer)
        resp = self.client.get(reverse('place_order', args=[self.shop.id]))
        self.assertEqual(resp.context['promo_rules'], [{'title': 'Rainy day sale', 'type': 'percent', 'value': 20.0, 'min': 100.0}])

    def test_suspended_shop_promos_not_public(self):
        self._promo(title='Suspended promo')
        self.shop.status = 'suspended'
        self.shop.save()
        self.assertEqual(self.client.get(reverse('shop_detail', args=[self.shop.id])).status_code, 404)
        self.assertNotContains(self.client.get(reverse('shop_list')), 'Suspended promo')
