"""Payment deadline: reminders, then auto-switch to cash on delivery (process_payment_reminders)."""
from datetime import timedelta
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from notifications.models import Notification
from orders.models import Order
from payments.models import PayMongoTransaction
from shops.models import Service, Shop

User = get_user_model()


@override_settings(PAYMENT_REMINDER_HOURS=[24, 48], PAYMENT_DUE_HOURS=72)
class PaymentDeadlineTests(TestCase):
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

    def _order(self, hours_since_clock_start, **kw):
        """An unpaid online order whose 72 h deadline clock started N hours ago."""
        fields = dict(customer=self.customer, shop=self.shop, pickup_address='P', delivery_address='D',
                      pickup_datetime=timezone.now() + timedelta(days=1), payment_method='gcash',
                      total_amount='200.00', status='washing',
                      payment_due_at=timezone.now() + timedelta(hours=72 - hours_since_clock_start))
        fields.update(kw)
        return Order.objects.create(**fields)

    def _run(self, *args):
        out = StringIO()
        call_command('process_payment_reminders', *args, stdout=out)
        return out.getvalue()

    def _notes(self, user):
        return list(Notification.objects.filter(user=user).values_list('message', flat=True))

    # ── clock starts at the right moments ──
    def test_clock_starts_at_booking_only_when_price_is_already_final(self):
        iron = Service.objects.create(shop=self.shop, name='iron', price_per_piece='15.00')
        self.client.force_login(self.customer)
        base = {'pickup_address': 'P', 'delivery_address': 'D',
                'pickup_datetime': (timezone.localtime() + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M')}
        # piece-only online order: priced at booking, deadline starts now
        self.client.post(reverse('place_order', args=[self.shop.id]),
                         {**base, 'payment_method': 'gcash', 'svc_selected': [iron.id], f'qty_{iron.id}': '3'})
        online = Order.objects.latest('id')
        self.assertAlmostEqual((online.payment_due_at - timezone.now()).total_seconds() / 3600, 72, delta=0.1)
        # per-kg order: weighed by the shop, so no deadline until weighing
        self.client.post(reverse('place_order', args=[self.shop.id]),
                         {**base, 'payment_method': 'gcash', 'svc_selected': [self.wash.id]})
        weighed = Order.objects.latest('id')
        self.assertTrue(weighed.price_pending)
        self.assertIsNone(weighed.payment_due_at)

    def test_clock_starts_at_weighing_and_restarts_on_reweigh(self):
        order = Order.objects.create(customer=self.customer, shop=self.shop, pickup_address='P', delivery_address='D',
                                     pickup_datetime=timezone.now() + timedelta(days=1), payment_method='gcash',
                                     status='picked_up', weigh_at_shop=True)
        line = order.order_services.create(service=self.wash, quantity=0, subtotal=0)
        self.assertIsNone(order.payment_due_at)
        self.client.force_login(self.owner)
        self.client.post(reverse('confirm_order_weight', args=[order.id]), {f'weight_{line.id}': '3'})
        order.refresh_from_db()
        self.assertIsNotNone(order.payment_due_at)
        Order.objects.filter(id=order.id).update(payment_reminders_sent=1)
        self.client.post(reverse('confirm_order_weight', args=[order.id]), {f'weight_{line.id}': '4'})
        order.refresh_from_db()
        self.assertEqual(order.payment_reminders_sent, 0)

    # ── reminders ──
    def test_no_reminder_before_24h(self):
        self._order(10)
        self._run()
        self.assertEqual(self._notes(self.customer), [])

    def test_first_reminder_once_then_second(self):
        order = self._order(25)
        self._run()
        self._run()                                            # idempotent: no duplicate
        notes = self._notes(self.customer)
        self.assertEqual(len(notes), 1)
        self.assertIn('Reminder', notes[0])
        self.assertIn('₱200.00', notes[0])
        Order.objects.filter(id=order.id).update(payment_due_at=timezone.now() + timedelta(hours=72 - 49))
        self._run()
        order.refresh_from_db()
        self.assertEqual((order.payment_reminders_sent, len(self._notes(self.customer))), (2, 2))

    def test_late_start_sends_one_catch_up_reminder(self):
        order = self._order(60)                                # both reminder times already passed
        self._run()
        order.refresh_from_db()
        self.assertEqual((order.payment_reminders_sent, len(self._notes(self.customer))), (2, 1))

    # ── deadline ──
    def test_overdue_order_switches_to_cod_and_notifies_both(self):
        order = self._order(73)
        out = self._run()
        order.refresh_from_db()
        self.assertEqual((order.payment_method, order.payment_due_at), ('cod', None))
        self.assertIsNone(order.delivery_block_reason())       # can now go out for delivery
        self.assertIn('cash on delivery', self._notes(self.customer)[0])
        self.assertIn('cash on delivery', self._notes(self.owner)[0])
        self.assertIn('switched to cash on delivery', out)
        self._run()                                            # second run: nothing more
        self.assertEqual(len(self._notes(self.customer)), 1)

    def test_paid_cod_and_closed_orders_are_ignored(self):
        self._order(80, payment_status='paid')
        self._order(80, payment_method='cod')
        self._order(80, status='cancelled')
        self._run()
        self.assertEqual(Notification.objects.count(), 0)

    def test_payment_in_progress_is_not_interrupted(self):
        order = self._order(80)
        PayMongoTransaction.objects.create(order=order, amount=20000, status='processing')
        self._run()
        order.refresh_from_db()
        self.assertEqual(order.payment_method, 'gcash')

    def test_dry_run_changes_nothing(self):
        order = self._order(80)
        out = self._run('--dry-run')
        order.refresh_from_db()
        self.assertEqual(order.payment_method, 'gcash')
        self.assertIn('[dry run]', out)
        self.assertEqual(Notification.objects.count(), 0)

    def test_deadline_shown_to_customer(self):
        order = self._order(30, weigh_at_shop=True, price_confirmed_at=timezone.now())
        self.client.force_login(self.customer)
        self.assertContains(self.client.get(reverse('order_detail', args=[order.id])), 'Please pay by')
