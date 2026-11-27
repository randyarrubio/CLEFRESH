"""Wash & Fold is a single service; legacy Wash / Fold rows are merged and hidden from booking."""
import importlib
from decimal import Decimal

from django.apps import apps
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from orders.models import Order, OrderService
from shops.models import Service, Shop

User = get_user_model()
merge = importlib.import_module('shops.migrations.0009_wash_and_fold_service').merge_wash_and_fold


class WashFoldTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username='o', email='o@e.com', password='x', role='shop_owner',
                                              email_verified=True)
        self.shop = Shop.objects.create(owner=self.owner, name='S', address='Cantilan', status='approved',
                                        latitude=9.3, longitude=125.9)

    def test_merge_combines_wash_and_unused_fold(self):
        Service.objects.create(shop=self.shop, name='wash', price_per_kg='50.00')
        Service.objects.create(shop=self.shop, name='fold', price_per_kg='20.00')
        merge(apps, None)
        names = set(self.shop.services.values_list('name', flat=True))
        self.assertEqual(names, {'wash_fold'})
        self.assertEqual(self.shop.services.get().price_per_kg, Decimal('70.00'))

    def test_merge_keeps_fold_with_order_history(self):
        wash = Service.objects.create(shop=self.shop, name='wash', price_per_kg='50.00')
        fold = Service.objects.create(shop=self.shop, name='fold', price_per_kg='20.00')
        customer = User.objects.create_user(username='c', email='c@e.com', password='x', role='customer')
        order = Order.objects.create(customer=customer, shop=self.shop, pickup_address='P', delivery_address='D',
                                     pickup_datetime=timezone.now(), payment_method='gcash')
        OrderService.objects.create(order=order, service=fold, weight_kg=2, subtotal=40)
        merge(apps, None)
        wash.refresh_from_db()
        self.assertEqual(wash.name, 'wash_fold')
        self.assertTrue(Service.objects.filter(pk=fold.pk).exists())

    def test_fold_only_shop_is_renamed(self):
        fold = Service.objects.create(shop=self.shop, name='fold', price_per_kg='25.00')
        merge(apps, None)
        fold.refresh_from_db()
        self.assertEqual(fold.name, 'wash_fold')

    def test_legacy_services_hidden_from_booking(self):
        legacy = Service.objects.create(shop=self.shop, name='fold', price_per_kg='20.00')
        Service.objects.create(shop=self.shop, name='wash_fold', price_per_kg='60.00')
        customer = User.objects.create_user(username='c', email='c@e.com', password='x', role='customer',
                                            face_verified_at=timezone.now(), id_status='verified')
        self.client.force_login(customer)
        page = self.client.get(reverse('place_order', args=[self.shop.id]))
        self.assertContains(page, 'Wash &amp; Fold')
        self.assertNotContains(page, f'value="{legacy.id}" id="svc-sel-{legacy.id}"')

    def test_owner_cannot_add_legacy_service(self):
        self.client.force_login(self.owner)
        self.client.post(reverse('create_service'), {'name': 'wash', 'price_per_kg': '50'})
        self.assertFalse(self.shop.services.exists())
        self.client.post(reverse('create_service'), {'name': 'wash_fold', 'price_per_load': '150'})
        self.assertEqual(self.shop.services.get().get_name_display(), 'Wash & Fold')
