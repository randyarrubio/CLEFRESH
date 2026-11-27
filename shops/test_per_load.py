"""Per-load pricing: washing services are charged per machine load, partial loads rounded up."""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from orders.models import Order
from shops.models import Service, Shop

User = get_user_model()


class LoadMathTests(TestCase):
    def setUp(self):
        owner = User.objects.create_user(username='o@e.com', email='o@e.com', password='Pass1234!', role='shop_owner')
        self.shop = Shop.objects.create(owner=owner, name='Suds', address='Cantilan', status='approved')

    def _svc(self, name, price):
        return Service.objects.create(shop=self.shop, name=name, price_per_load=price, load_kg=Service.LOAD_KG[name])

    def test_load_sizes(self):
        self.assertEqual(Service.LOAD_KG, {'wash_fold': Decimal('5'), 'bedding': Decimal('4'),
                                           'comforter': Decimal('4')})

    def test_clothes_round_up_to_5kg_loads(self):
        wash = self._svc('wash_fold', '150.00')
        self.assertEqual(wash.loads_for(Decimal('10')), 2)
        self.assertEqual(wash.loads_for(Decimal('10.5')), 3)
        self.assertEqual(wash.loads_for(Decimal('1')), 1)
        self.assertEqual(wash.weight_charge(Decimal('10')), Decimal('300.00'))
        self.assertEqual(wash.weight_charge(Decimal('10.5')), Decimal('450.00'))
        self.assertEqual(wash.rate_label, '₱150.00 per 5 kg load')

    def test_bedding_and_comforters_use_4kg_loads(self):
        for name in ('bedding', 'comforter'):
            svc = self._svc(name, '200.00')
            self.assertEqual(svc.loads_for(Decimal('8')), 2)
            self.assertEqual(svc.loads_for(Decimal('8.1')), 3)
            self.assertEqual(svc.weight_charge(Decimal('5')), Decimal('400.00'))

    def test_per_kg_services_still_work(self):
        dry = Service.objects.create(shop=self.shop, name='dry', price_per_kg='30.00')
        self.assertFalse(dry.per_load)
        self.assertIsNone(dry.loads_for(Decimal('3')))
        self.assertEqual(dry.weight_charge(Decimal('3.5')), Decimal('105.00'))


@override_settings(COD_TRUSTED_MIN_ORDERS=0)
class PerLoadFlowTests(TestCase):
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
        self.owner = mk('o@e.com', 'shop_owner')
        self.customer = mk('c@e.com', 'customer')
        self.customer.face_verified_at = timezone.now()
        self.customer.id_status = 'verified'
        self.customer.save(update_fields=['face_verified_at', 'id_status'])
        self.shop = Shop.objects.create(owner=self.owner, name='Suds', address='Cantilan', status='approved',
                                        latitude=9.3, longitude=125.9)
        self.wash = Service.objects.create(shop=self.shop, name='wash_fold', price_per_load='150.00', load_kg='5')
        self.comforter = Service.objects.create(shop=self.shop, name='comforter', price_per_load='260.00', load_kg='4')

    def test_walk_in_counts_loads(self):
        self.client.force_login(self.owner)
        self.client.post(reverse('shop_pos'), {
            'walk_in_name': 'Maria', 'payment': 'counter_paid',
            f'weight_{self.wash.id}': '10', f'weight_{self.comforter.id}': '4.5'})
        order = Order.objects.get()
        self.assertEqual(order.total_amount, Decimal('820.00'))      # 2 x 150 + 2 x 260
        lines = {l.service.name: l for l in order.order_services.select_related('service')}
        self.assertEqual(lines['wash_fold'].loads, 2)
        self.assertEqual(lines['comforter'].loads, 2)
        receipt = self.client.get(reverse('pos_receipt', args=[order.id]))
        self.assertContains(receipt, '(2 loads)')

    def test_booked_order_weighed_into_loads(self):
        self.client.force_login(self.customer)
        self.client.post(reverse('place_order', args=[self.shop.id]), {
            'pickup_address': 'P', 'delivery_address': 'D', 'payment_method': 'gcash', 'weigh_at_shop': '1',
            'pickup_datetime': (timezone.localdate() + timedelta(days=1)).strftime('%Y-%m-%dT10:00'),
            'svc_selected': [self.wash.id]})
        order = Order.objects.get()
        self.assertTrue(order.price_pending)
        line = order.order_services.get()
        Order.objects.filter(pk=order.pk).update(status='picked_up')    # dropped off at the shop
        self.client.force_login(self.owner)
        self.client.post(reverse('confirm_order_weight', args=[order.id]), {f'weight_{line.id}': '10.5'})
        order.refresh_from_db()
        line.refresh_from_db()
        self.assertEqual(line.loads, 3)
        self.assertEqual(order.total_amount, Decimal('450.00'))
        self.client.force_login(self.customer)
        self.assertContains(self.client.get(reverse('order_detail', args=[order.id])), '3 loads')

    def test_owner_adds_per_load_service(self):
        self.client.force_login(self.owner)
        self.client.post(reverse('create_service'), {'name': 'bedding', 'price_per_load': '180'})
        svc = self.shop.services.get(name='bedding')
        self.assertEqual(svc.price_per_load, Decimal('180.00'))
        self.assertEqual(svc.load_kg, Decimal('4.00'))
        self.assertIsNone(svc.price_per_kg)

    def test_shop_page_shows_load_price(self):
        self.assertContains(self.client.get(reverse('shop_detail', args=[self.shop.id])), 'per 5 kg load')
