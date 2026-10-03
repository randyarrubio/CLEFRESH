"""Cash-on-delivery customers can still pay online once the shop has weighed their laundry."""
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from orders.models import Order
from payments.models import PayMongoTransaction
from shops.models import Service, Shop

User = get_user_model()


def _resp(status, payload):
    r = mock.Mock(status_code=status)
    r.json.return_value = payload
    return r


def _paymongo(attach_status):
    """Fake requests.post for intent -> method -> attach."""
    attach = {'data': {'attributes': {'status': attach_status, 'payments': [{'id': 'pay_1'}],
                                      'next_action': {'redirect': {'url': 'https://pm.test/redirect'}}}}}
    return mock.patch('payments.views.requests.post', side_effect=[
        _resp(200, {'data': {'id': 'pi_1'}}), _resp(200, {'data': {'id': 'pm_1'}}), _resp(200, attach)])


@override_settings(PAYMONGO_SECRET_KEY='sk_test')
class CodPayOnlineTests(TestCase):
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
        self.owner = mk('o@e.com', 'shop_owner')
        self.shop = Shop.objects.create(owner=self.owner, name='Suds', address='Tandag', status='approved')
        self.wash = Service.objects.create(shop=self.shop, name='wash', price_per_kg='50.00')
        self.order = Order.objects.create(
            customer=self.customer, shop=self.shop, pickup_address='P', delivery_address='D',
            pickup_datetime=timezone.now() + timedelta(days=1), payment_method='cod',
            status='washing', weigh_at_shop=True, price_confirmed_at=timezone.now(), total_amount='200.00')
        self.client.force_login(self.customer)

    def _pay(self, method='gcash'):
        return self.client.post(reverse('payment_process', args=[self.order.id]), {'payment_method_type': method})

    def test_weighed_cod_order_offers_online_payment(self):
        self.assertTrue(self.order.can_pay_online)
        self.assertEqual(self.client.get(reverse('payment_checkout', args=[self.order.id])).status_code, 200)
        self.assertContains(self.client.get(reverse('order_detail', args=[self.order.id])), 'online</a>')

    def test_not_before_weighing(self):
        Order.objects.filter(id=self.order.id).update(price_confirmed_at=None)
        self.order.refresh_from_db()
        self.assertFalse(self.order.can_pay_online)
        resp = self.client.get(reverse('payment_checkout', args=[self.order.id]))
        self.assertRedirects(resp, reverse('order_detail', args=[self.order.id]), fetch_redirect_response=False)

    def test_not_once_out_for_delivery(self):
        Order.objects.filter(id=self.order.id).update(status='out_for_delivery')
        for url in (reverse('payment_checkout', args=[self.order.id]), reverse('payment_process', args=[self.order.id])):
            resp = self.client.post(url, {'payment_method_type': 'gcash'})
            self.assertRedirects(resp, reverse('order_detail', args=[self.order.id]), fetch_redirect_response=False)
        self.assertFalse(PayMongoTransaction.objects.exists())
        self.assertNotContains(self.client.get(reverse('order_detail', args=[self.order.id])),
                               reverse('payment_checkout', args=[self.order.id]))

    def test_successful_payment_records_the_ewallet(self):
        with _paymongo('succeeded'):
            self._pay('maya')
        self.order.refresh_from_db()
        self.assertEqual((self.order.payment_status, self.order.payment_method), ('paid', 'maya'))
        tx = PayMongoTransaction.objects.get(order=self.order)
        self.assertEqual((tx.amount, tx.payment_method_type), (20000, 'maya'))   # centavos

    def test_redirect_attempt_keeps_cod_and_blocks_delivery_until_resolved(self):
        with _paymongo('awaiting_next_action'):
            resp = self._pay('gcash')
        self.assertEqual(resp['Location'], 'https://pm.test/redirect')
        self.order.refresh_from_db()
        self.assertEqual(self.order.payment_method, 'cod')
        self.assertIsNotNone(self.order.delivery_block_reason())    # don't dispatch mid-payment
        # Customer abandons the e-wallet page: attempt fails, cash on delivery still works.
        PayMongoTransaction.objects.filter(order=self.order).update(status='failed')
        self.assertIsNone(self.order.delivery_block_reason())
        self.assertEqual(self.order.payment_method, 'cod')

    def test_webhook_style_success_converts_cod(self):
        from payments.views import _mark_order_paid
        PayMongoTransaction.objects.create(order=self.order, payment_intent_id='pi_9', amount=20000,
                                           status='processing', payment_method_type='grab_pay')
        self.assertTrue(_mark_order_paid(self.order, 'pi_9', 'pay_9'))
        self.order.refresh_from_db()
        self.assertEqual((self.order.payment_status, self.order.payment_method), ('paid', 'grab_pay'))
        self.assertFalse(self.order.can_pay_online)
