from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.models import RiderProfile
from orders.models import Order, OrderStatusLog, PhotoProof
from shops.models import DeliverySlot, Service, Shop


User = get_user_model()


class SystemHardeningTests(TestCase):
    def setUp(self):
        self.customer = User.objects.create_user(
            username='customer@example.com',
            email='customer@example.com',
            password='pass12345',
            role='customer',
        )
        self.owner = User.objects.create_user(
            username='owner@example.com',
            email='owner@example.com',
            password='pass12345',
            role='shop_owner',
        )
        self.other_owner = User.objects.create_user(
            username='other-owner@example.com',
            email='other-owner@example.com',
            password='pass12345',
            role='shop_owner',
        )
        self.rider = User.objects.create_user(
            username='rider@example.com',
            email='rider@example.com',
            password='pass12345',
            role='rider',
        )
        RiderProfile.objects.create(user=self.rider, is_submitted=True, approval_status='approved')
        self.shop = Shop.objects.create(
            owner=self.owner,
            name='Clean Test Shop',
            address='123 Test Street',
            status='approved',
        )
        self.service = Service.objects.create(
            shop=self.shop,
            name='wash',
            price_per_kg='50.00',
        )
        self.slot = DeliverySlot.objects.create(
            shop=self.shop,
            date=timezone.now().date() + timedelta(days=1),
            time_start='09:00',
            time_end='11:00',
            max_capacity=1,
        )

    def test_login_next_external_url_is_sanitized(self):
        response = self.client.post(
            reverse('login') + '?next=https://evil.example/path',
            {'username': self.customer.email, 'password': 'pass12345'},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], '/')

    def test_slots_api_returns_available_slots(self):
        response = self.client.get(reverse('api_slots', args=[self.shop.id]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['slots'][0]['id'], self.slot.id)

    def test_shop_site_nav_links_stay_on_current_shop(self):
        response = self.client.get(reverse('shop_site_services', args=[self.shop.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse('shop_detail', args=[self.shop.id]))
        self.assertContains(response, reverse('shop_site_services', args=[self.shop.id]))
        self.assertContains(response, reverse('shop_site_how_it_works', args=[self.shop.id]))
        self.assertContains(response, reverse('shop_site_about', args=[self.shop.id]))
        self.assertContains(response, f'{self.shop.name} Services and Pricing')

    def test_shop_login_to_order_uses_login_modal(self):
        response = self.client.get(reverse('shop_detail', args=[self.shop.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Login to Order')
        self.assertContains(response, 'data-login-modal')
        self.assertContains(response, 'globalLoginModal')
        self.assertContains(response, f"{reverse('login')}?next={reverse('shop_detail', args=[self.shop.id])}")

    def test_shop_owner_dashboard_nav_uses_owner_links(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse('shop_dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse('shop_dashboard_orders'))
        self.assertContains(response, reverse('shop_dashboard_slots'))
        self.assertContains(response, reverse('shop_dashboard_services'))
        self.assertContains(response, reverse('shop_dashboard_analytics'))
        self.assertContains(response, reverse('shop_dashboard_payments'))

    def test_home_page_switches_to_shop_owner_workspace(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse('home'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Shop owner workspace')
        self.assertContains(response, self.shop.name)
        self.assertContains(response, reverse('shop_dashboard'))
        self.assertContains(response, reverse('shop_dashboard_orders'))
        self.assertContains(response, reverse('shop_dashboard_slots'))
        self.assertContains(response, reverse('shop_dashboard_services'))
        self.assertContains(response, reverse('shop_dashboard_analytics'))
        self.assertContains(response, reverse('shop_dashboard_payments'))
        self.assertNotContains(response, 'My Shop')
        self.assertNotContains(response, 'Featured Laundry Providers')

    def test_public_home_keeps_marketplace_landing_content(self):
        response = self.client.get(reverse('home'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'site-water-bg')
        self.assertContains(response, 'Featured Laundry Providers')
        self.assertContains(response, '/static/img/machine%20wash.png')
        self.assertContains(response, '/static/img/dry%20cleaning.png')
        self.assertContains(response, '/static/img/hand%20wash.png')
        self.assertContains(response, '/static/img/iron%20%26%20press.png')
        self.assertContains(response, '/static/img/express%20service.png')
        self.assertContains(response, '/static/img/shoe%20cleaning.png')
        self.assertNotContains(response, 'Shop owner workspace')

    def test_customer_home_switches_to_shop_discovery(self):
        self.client.force_login(self.customer)
        response = self.client.get(reverse('home'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Search laundry shops')
        self.assertContains(response, 'Find a shop that fits today')
        self.assertContains(response, 'All Shops')
        self.assertContains(response, 'Nearby shops')
        self.assertContains(response, 'Book')
        self.assertContains(response, self.shop.name)
        self.assertContains(response, reverse('order_list'))
        content = response.content.decode()
        topbar_nav = content.split('<nav class="topbar-nav" id="topbarNav">', 1)[1].split('</nav>', 1)[0]
        mobile_nav = content.split('<div class="mobile-menu" id="mobileMenu">', 1)[1].split('</div>', 1)[0]
        self.assertNotIn(reverse('home') + '#services', topbar_nav)
        self.assertNotIn(reverse('home') + '#how-it-works', topbar_nav)
        self.assertNotIn(reverse('home') + '#about-us', topbar_nav)
        self.assertNotIn(reverse('home') + '#services', mobile_nav)
        self.assertNotIn(reverse('home') + '#how-it-works', mobile_nav)
        self.assertNotIn(reverse('home') + '#about-us', mobile_nav)
        self.assertNotContains(response, 'Featured Laundry Providers')
        self.assertNotContains(response, 'Shop owner workspace')

    def test_customer_home_search_filters_shops(self):
        self.client.force_login(self.customer)
        response = self.client.get(reverse('home'), {'q': 'missing-shop'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'No shops found')
        self.assertNotContains(response, self.shop.name)

    def test_order_status_api_blocks_unrelated_shop_owner(self):
        order = Order.objects.create(
            customer=self.customer,
            shop=self.shop,
            pickup_address='Pickup',
            delivery_address='Delivery',
            pickup_datetime=timezone.now() + timedelta(days=1),
            payment_method='cod',
            total_amount='50.00',
        )
        OrderStatusLog.objects.create(order=order, status='pending')
        self.client.force_login(self.other_owner)

        response = self.client.get(f'/api/orders/{order.id}/status/')
        self.assertEqual(response.status_code, 404)

    def test_my_orders_new_order_links_target_order_shop(self):
        Order.objects.create(
            customer=self.customer,
            shop=self.shop,
            pickup_address='Pickup',
            delivery_address='Delivery',
            pickup_datetime=timezone.now() + timedelta(days=1),
            payment_method='cod',
            total_amount='50.00',
        )
        self.client.force_login(self.customer)

        response = self.client.get(reverse('order_list'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse('place_order', args=[self.shop.id]))

        content = response.content.decode()
        topbar_nav = content.split('<nav class="topbar-nav" id="topbarNav">', 1)[1].split('</nav>', 1)[0]
        mobile_nav = content.split('<div class="mobile-menu" id="mobileMenu">', 1)[1].split('</div>', 1)[0]
        self.assertNotIn(reverse('home') + '#services', topbar_nav)
        self.assertNotIn(reverse('home') + '#how-it-works', topbar_nav)
        self.assertNotIn(reverse('home') + '#about-us', topbar_nav)
        self.assertNotIn(reverse('home') + '#services', mobile_nav)
        self.assertNotIn(reverse('home') + '#how-it-works', mobile_nav)
        self.assertNotIn(reverse('home') + '#about-us', mobile_nav)

    def test_negative_order_values_are_rejected_without_creating_order(self):
        self.client.force_login(self.customer)
        pickup_datetime = (timezone.now() + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M')
        response = self.client.post(
            reverse('place_order', args=[self.shop.id]),
            {
                'pickup_address': 'Pickup',
                'delivery_address': 'Delivery',
                'pickup_datetime': pickup_datetime,
                'payment_method': 'cod',
                f'weight_{self.service.id}': '-1',
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Order.objects.exists())

    def test_place_order_uses_customer_nav_without_landing_links(self):
        self.client.force_login(self.customer)
        response = self.client.get(reverse('place_order', args=[self.shop.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, f'Book with {self.shop.name}')
        self.assertContains(response, 'Choose services')
        self.assertContains(response, 'Next: details')
        self.assertContains(response, 'Choose schedule')
        self.assertContains(response, 'Payment method')

        content = response.content.decode()
        topbar_nav = content.split('<nav class="topbar-nav" id="topbarNav">', 1)[1].split('</nav>', 1)[0]
        mobile_nav = content.split('<div class="mobile-menu" id="mobileMenu">', 1)[1].split('</div>', 1)[0]
        self.assertNotIn(reverse('home') + '#services', topbar_nav)
        self.assertNotIn(reverse('home') + '#how-it-works', topbar_nav)
        self.assertNotIn(reverse('home') + '#about-us', topbar_nav)
        self.assertNotIn(reverse('home') + '#services', mobile_nav)
        self.assertNotIn(reverse('home') + '#how-it-works', mobile_nav)
        self.assertNotIn(reverse('home') + '#about-us', mobile_nav)

    def test_rider_workflow_pages_render_assigned_job(self):
        order = Order.objects.create(
            customer=self.customer,
            shop=self.shop,
            rider=self.rider,
            pickup_address='Pickup',
            delivery_address='Delivery',
            pickup_datetime=timezone.now() + timedelta(days=1),
            payment_method='cod',
            total_amount='50.00',
            status='accepted',
        )
        OrderStatusLog.objects.create(order=order, status='accepted')
        self.client.force_login(self.rider)

        dashboard = self.client.get(reverse('rider_dashboard'))
        self.assertEqual(dashboard.status_code, 200)
        self.assertContains(dashboard, 'Rider workflow')
        self.assertContains(dashboard, 'Active job requests')
        self.assertContains(dashboard, reverse('rider_task_detail', args=[order.id]))
        content = dashboard.content.decode()
        topbar_nav = content.split('<nav class="topbar-nav" id="topbarNav">', 1)[1].split('</nav>', 1)[0]
        mobile_nav = content.split('<div class="mobile-menu" id="mobileMenu">', 1)[1].split('</div>', 1)[0]
        self.assertNotIn(f'href="{reverse("home")}">Home', topbar_nav)
        self.assertNotIn(reverse('home') + '#services', topbar_nav)
        self.assertNotIn(reverse('home') + '#how-it-works', topbar_nav)
        self.assertNotIn(reverse('home') + '#about-us', topbar_nav)
        self.assertNotIn(f'href="{reverse("home")}" onclick="toggleMobileMenu()">Home', mobile_nav)
        self.assertNotIn(reverse('home') + '#services', mobile_nav)
        self.assertNotIn(reverse('home') + '#how-it-works', mobile_nav)
        self.assertNotIn(reverse('home') + '#about-us', mobile_nav)

        detail = self.client.get(reverse('rider_task_detail', args=[order.id]))
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, 'Confirm pickup')
        self.assertContains(detail, 'Photo proof')
        self.assertContains(detail, reverse('rider_update_status', args=[order.id]))

    def test_rider_dashboard_shows_unassigned_customer_orders(self):
        order = Order.objects.create(
            customer=self.customer,
            shop=self.shop,
            pickup_address='Pickup',
            delivery_address='Delivery',
            pickup_datetime=timezone.now() + timedelta(days=1),
            payment_method='cod',
            total_amount='50.00',
            status='accepted',
        )
        self.client.force_login(self.rider)

        response = self.client.get(reverse('rider_dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'New job alerts')
        self.assertContains(response, 'Accept job')
        self.assertContains(response, reverse('rider_accept_job', args=[order.id]))
        self.assertContains(response, f'Order #{order.id}')

    def test_rider_can_accept_unassigned_customer_order(self):
        order = Order.objects.create(
            customer=self.customer,
            shop=self.shop,
            pickup_address='Pickup',
            delivery_address='Delivery',
            pickup_datetime=timezone.now() + timedelta(days=1),
            payment_method='cod',
            total_amount='50.00',
            status='accepted',
        )
        self.client.force_login(self.rider)

        response = self.client.post(reverse('rider_accept_job', args=[order.id]))
        self.assertRedirects(response, reverse('rider_task_detail', args=[order.id]))
        order.refresh_from_db()
        self.assertEqual(order.rider, self.rider)
        # Claiming a job assigns the rider only; the shop already accepted the order.
        self.assertEqual(order.status, 'accepted')

        detail = self.client.get(reverse('rider_task_detail', args=[order.id]))
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, 'Confirm pickup')

    def test_rider_messages_render_as_modal(self):
        order = Order.objects.create(
            customer=self.customer,
            shop=self.shop,
            pickup_address='Pickup',
            delivery_address='Delivery',
            pickup_datetime=timezone.now() + timedelta(days=1),
            payment_method='cod',
            total_amount='50.00',
            status='accepted',
        )
        self.client.force_login(self.rider)

        response = self.client.post(reverse('rider_accept_job', args=[order.id]), follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'riderMessageModal')
        self.assertContains(response, f'Order #{order.id} accepted.')
        self.assertNotContains(response, 'message-shell')

    def test_rider_status_updates_require_matching_photo_proofs(self):
        order = Order.objects.create(
            customer=self.customer,
            shop=self.shop,
            rider=self.rider,
            pickup_address='Pickup',
            delivery_address='Delivery',
            pickup_datetime=timezone.now() + timedelta(days=1),
            payment_method='cod',
            total_amount='50.00',
            status='accepted',
        )
        self.client.force_login(self.rider)

        response = self.client.post(reverse('rider_update_status', args=[order.id]), {'status': 'picked_up'})
        self.assertRedirects(response, reverse('rider_task_detail', args=[order.id]))
        order.refresh_from_db()
        self.assertEqual(order.status, 'accepted')

        PhotoProof.objects.create(order=order, photo='proof_photos/pickup.jpg', proof_type='pickup', uploaded_by=self.rider)
        self.client.post(reverse('rider_update_status', args=[order.id]), {'status': 'picked_up'})
        order.refresh_from_db()
        self.assertEqual(order.status, 'picked_up')

        self.client.post(reverse('rider_update_status', args=[order.id]), {'status': 'out_for_delivery'})
        order.refresh_from_db()
        self.assertEqual(order.status, 'picked_up')

        PhotoProof.objects.create(order=order, photo='proof_photos/dropoff.jpg', proof_type='dropoff', uploaded_by=self.rider)
        self.client.post(reverse('rider_update_status', args=[order.id]), {'status': 'out_for_delivery'})
        order.refresh_from_db()
        self.assertEqual(order.status, 'out_for_delivery')

        self.client.post(reverse('rider_update_status', args=[order.id]), {'status': 'delivered'})
        order.refresh_from_db()
        self.assertEqual(order.status, 'out_for_delivery')

        PhotoProof.objects.create(order=order, photo='proof_photos/delivery.jpg', proof_type='delivery', uploaded_by=self.rider)
        self.client.post(reverse('rider_update_status', args=[order.id]), {'status': 'delivered', 'cod_collected': 'on'})
        order.refresh_from_db()
        self.assertEqual(order.status, 'delivered')
        self.assertEqual(order.payment_status, 'paid')

    def test_rider_task_detail_opens_modal_for_missing_required_proof(self):
        order = Order.objects.create(
            customer=self.customer,
            shop=self.shop,
            rider=self.rider,
            pickup_address='Pickup',
            delivery_address='Delivery',
            pickup_datetime=timezone.now() + timedelta(days=1),
            payment_method='cod',
            total_amount='50.00',
            status='accepted',
        )
        self.client.force_login(self.rider)

        response = self.client.get(reverse('rider_task_detail', args=[order.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Upload a pickup proof photo before confirming pickup.')
        self.assertContains(response, 'Confirm pickup</button>', html=False)
        self.assertContains(response, 'riderProofWarningModal')
        self.assertContains(response, 'data-rider-proof-warning="Upload a pickup proof photo before confirming pickup."')
        self.assertNotContains(response, '<select name="proof_type">')
        self.assertContains(response, 'name="proof_type" value="pickup"')

        PhotoProof.objects.create(order=order, photo='proof_photos/pickup.jpg', proof_type='pickup', uploaded_by=self.rider)
        response = self.client.get(reverse('rider_task_detail', args=[order.id]))
        self.assertNotContains(response, 'Upload a pickup proof photo before confirming pickup.')

        order.status = 'out_for_delivery'
        order.save(update_fields=['status'])
        response = self.client.get(reverse('rider_task_detail', args=[order.id]))
        self.assertContains(response, 'Upload a delivery proof photo before confirming delivery.')
        self.assertContains(response, 'data-rider-proof-warning="Upload a delivery proof photo before confirming delivery."')

    def test_geocode_proxy_requires_authentication(self):
        response = self.client.get('/api/geocode/?address=Test')
        self.assertEqual(response.status_code, 401)

    @override_settings(PAYMONGO_WEBHOOK_SECRET='')
    def test_paymongo_webhook_requires_configured_secret(self):
        response = self.client.post(reverse('paymongo_webhook'), data='{}', content_type='application/json')
        self.assertEqual(response.status_code, 503)
