"""'Let the shop weigh it': book without a weight, shop weighs at drop-off, customer pays before delivery."""
import io
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import RiderProfile
from notifications.models import Notification
from orders.models import Order, PhotoProof
from payments.models import PayMongoTransaction
from shops.models import Promotion, Service, Shop

User = get_user_model()


def _png():
    from PIL import Image
    buf = io.BytesIO()
    Image.new('RGB', (2, 2)).save(buf, format='PNG')
    return SimpleUploadedFile('scale.png', buf.getvalue(), content_type='image/png')


class WeighAtShopTests(TestCase):
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
        self.rider = mk('r@e.com', 'rider')
        RiderProfile.objects.create(user=self.rider, is_submitted=True, approval_status='approved')
        self.shop = Shop.objects.create(owner=self.owner, name='Suds', address='Tandag', status='approved')
        self.wash = Service.objects.create(shop=self.shop, name='wash', price_per_kg='50.00')
        self.iron = Service.objects.create(shop=self.shop, name='iron', price_per_piece='15.00')

    # ── helpers ──
    def _book(self, payment='gcash', weigh=True, selected=None, **extra):
        data = {'pickup_address': 'P', 'delivery_address': 'D', 'payment_method': payment,
                'pickup_datetime': (timezone.localtime() + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M'),
                'weigh_at_shop': '1' if weigh else '0',
                'svc_selected': selected if selected is not None else [self.wash.id]}
        data.update(extra)
        self.client.force_login(self.customer)
        resp = self.client.post(reverse('place_order', args=[self.shop.id]), data)
        return resp, Order.objects.filter(customer=self.customer).order_by('-id').first()

    def _weigh(self, order, kg='4.20', **extra):
        self.client.force_login(self.owner)
        line = order.order_services.get(service=self.wash)
        data = {f'weight_{line.id}': kg}
        data.update(extra)
        return self.client.post(reverse('confirm_order_weight', args=[order.id]), data)

    # ── booking ──
    def test_booking_without_weight_creates_pending_price(self):
        resp, order = self._book(**{f'qty_{self.iron.id}': '2'}, selected=[self.wash.id, self.iron.id])
        self.assertRedirects(resp, reverse('order_detail', args=[order.id]))      # not sent to checkout
        self.assertTrue(order.weigh_at_shop)
        self.assertTrue(order.price_pending)
        wash_line = order.order_services.get(service=self.wash)
        self.assertIsNone(wash_line.weight_kg)
        self.assertEqual(str(order.total_amount), '30.00')                       # only the known piece part
        self.assertEqual(self.client.get(reverse('order_list')).content.decode().count('To be weighed'), 1)

    def test_weigh_mode_with_only_piece_items_is_priced_normally(self):
        resp, order = self._book(selected=[self.iron.id], **{f'qty_{self.iron.id}': '3'})
        self.assertFalse(order.weigh_at_shop)
        self.assertEqual(str(order.total_amount), '45.00')

    def test_promo_not_applied_at_booking(self):
        Promotion.objects.create(shop=self.shop, title='20%', discount_type='percent', discount_value=20,
                                 start_date=timezone.localdate(), end_date=timezone.localdate())
        _resp, order = self._book()
        self.assertIsNone(order.promotion)

    def test_cannot_pay_before_weighing(self):
        _resp, order = self._book()
        self.assertRedirects(self.client.get(reverse('payment_checkout', args=[order.id])),
                             reverse('order_detail', args=[order.id]))
        with override_settings(PAYMONGO_SECRET_KEY='sk_test'), \
                mock.patch('payments.views.requests.post') as post:
            self.client.post(reverse('payment_process', args=[order.id]), {'payment_method_type': 'gcash'})
        post.assert_not_called()
        self.assertNotContains(self.client.get(reverse('order_detail', args=[order.id])), 'Pay ₱')

    # ── weighing ──
    def test_shop_cannot_weigh_before_drop_off(self):
        _resp, order = self._book()
        Order.objects.filter(id=order.id).update(status='accepted')
        self._weigh(order)
        order.refresh_from_db()
        self.assertTrue(order.price_pending)

    def test_shop_confirms_weight_and_customer_is_notified(self):
        Promotion.objects.create(shop=self.shop, title='Ten off', discount_type='fixed', discount_value=10,
                                 start_date=timezone.localdate(), end_date=timezone.localdate())
        _resp, order = self._book()
        Order.objects.filter(id=order.id).update(status='picked_up')
        self._weigh(order, kg='4.20', scale_photo=_png())
        order.refresh_from_db()
        self.assertFalse(order.price_pending)
        self.assertEqual(order.price_confirmed_by, self.owner)
        self.assertEqual((str(order.subtotal_amount), str(order.discount_amount), str(order.total_amount)),
                         ('210.00', '10.00', '200.00'))                          # promo applied at weighing
        self.assertEqual(str(order.order_services.get(service=self.wash).weight_kg), '4.20')
        self.assertTrue(PhotoProof.objects.filter(order=order, proof_type='weight').exists())
        note = Notification.objects.filter(user=self.customer).latest('id').message
        self.assertIn('4.2 kg', note)
        self.assertIn('Please pay by', note)
        self.client.force_login(self.customer)
        page = self.client.get(reverse('order_detail', args=[order.id]))
        self.assertContains(page, 'Final price confirmed')
        self.assertContains(page, reverse('payment_checkout', args=[order.id]))

    def test_invalid_weights_rejected(self):
        _resp, order = self._book()
        Order.objects.filter(id=order.id).update(status='picked_up')
        for bad in ('', '0', '-1', 'abc', '500'):
            self._weigh(order, kg=bad)
            order.refresh_from_db()
            self.assertTrue(order.price_pending, bad)

    def test_other_shop_cannot_weigh(self):
        _resp, order = self._book()
        Order.objects.filter(id=order.id).update(status='picked_up')
        other = User.objects.create_user(username='x@e.com', email='x@e.com', password='x', role='shop_owner', email_verified=True)
        Shop.objects.create(owner=other, name='Other', address='A', status='approved')
        self.client.force_login(other)
        self.assertEqual(self.client.post(reverse('confirm_order_weight', args=[order.id]), {}).status_code, 404)

    def test_price_locked_once_paid_or_payment_in_progress(self):
        _resp, order = self._book()
        Order.objects.filter(id=order.id).update(status='picked_up')
        self._weigh(order, kg='2.00')
        PayMongoTransaction.objects.create(order=order, amount=10000, status='awaiting_payment_method')
        self._weigh(order, kg='9.00')
        order.refresh_from_db()
        self.assertEqual(str(order.total_amount), '100.00')
        PayMongoTransaction.objects.filter(order=order).delete()
        Order.objects.filter(id=order.id).update(payment_status='paid')
        self._weigh(order, kg='9.00')
        order.refresh_from_db()
        self.assertEqual(str(order.total_amount), '100.00')

    def test_reweigh_corrects_price_before_payment(self):
        _resp, order = self._book()
        Order.objects.filter(id=order.id).update(status='washing')
        self._weigh(order, kg='2.00')
        self._weigh(order, kg='3.00')
        order.refresh_from_db()
        self.assertEqual(str(order.total_amount), '150.00')

    # ── pay before delivery ──
    def _ready(self, order, **fields):
        Order.objects.filter(id=order.id).update(status='ready', rider=self.rider, **fields)

    def test_shop_cannot_send_out_unweighed_or_unpaid(self):
        _resp, order = self._book()
        self._ready(order)
        self.client.force_login(self.owner)
        self.client.post(reverse('update_order_status', args=[order.id]), {'status': 'out_for_delivery'})
        order.refresh_from_db()
        self.assertEqual(order.status, 'ready')                                  # not weighed
        self._weigh(order)
        self.client.post(reverse('update_order_status', args=[order.id]), {'status': 'out_for_delivery'})
        order.refresh_from_db()
        self.assertEqual(order.status, 'ready')                                  # weighed, unpaid online
        Order.objects.filter(id=order.id).update(payment_status='paid')
        self.client.post(reverse('update_order_status', args=[order.id]), {'status': 'out_for_delivery'})
        order.refresh_from_db()
        self.assertEqual(order.status, 'out_for_delivery')

    def test_rider_cannot_start_delivery_until_paid(self):
        _resp, order = self._book()
        self._ready(order)
        self._weigh(order)
        PhotoProof.objects.create(order=order, photo='proof_photos/x.png', proof_type='dropoff', uploaded_by=self.rider)
        self.client.force_login(self.rider)
        self.client.post(reverse('rider_update_status', args=[order.id]), {'status': 'out_for_delivery'})
        order.refresh_from_db()
        self.assertEqual(order.status, 'ready')

    def test_cod_weighed_order_can_go_out(self):
        _resp, order = self._book(payment='cod')
        self._ready(order)
        self._weigh(order)
        self.client.force_login(self.owner)
        self.client.post(reverse('update_order_status', args=[order.id]), {'status': 'out_for_delivery'})
        order.refresh_from_db()
        self.assertEqual(order.status, 'out_for_delivery')

    # ── switch to cash on delivery ──
    def test_customer_switches_to_cod(self):
        _resp, order = self._book()
        self._ready(order)
        self._weigh(order)
        self.client.force_login(self.customer)
        self.client.post(reverse('switch_to_cod', args=[order.id]))
        order.refresh_from_db()
        self.assertEqual(order.payment_method, 'cod')
        self.assertIsNone(order.delivery_block_reason())

    def test_switch_to_cod_blocked_when_paid_or_out(self):
        _resp, order = self._book()
        Order.objects.filter(id=order.id).update(payment_status='paid')
        self.client.force_login(self.customer)
        self.client.post(reverse('switch_to_cod', args=[order.id]))
        order.refresh_from_db()
        self.assertEqual(order.payment_method, 'gcash')
        Order.objects.filter(id=order.id).update(payment_status='unpaid', status='out_for_delivery')
        self.client.post(reverse('switch_to_cod', args=[order.id]))
        order.refresh_from_db()
        self.assertEqual(order.payment_method, 'gcash')

    def test_customer_typed_weight_is_ignored(self):
        # Weighing is shop-only: even a weight sent by an old/tampered form doesn't price the order.
        resp, order = self._book(weigh=False, selected=[], **{f'weight_{self.wash.id}': '2'})
        self.assertTrue(order.weigh_at_shop)
        self.assertTrue(order.price_pending)
        self.assertIsNone(order.order_services.get(service=self.wash).weight_kg)
        self.assertRedirects(resp, reverse('order_detail', args=[order.id]))

    def test_booking_page_has_no_weight_inputs(self):
        self.client.force_login(self.customer)
        page = self.client.get(reverse('place_order', args=[self.shop.id])).content.decode()
        self.assertNotIn(f'name="weight_{self.wash.id}" min=', page)
        self.assertNotIn('I know the weight', page)
        self.assertIn('The shop weighs it', page)
