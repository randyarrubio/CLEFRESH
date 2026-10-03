"""Follow up on unpaid online orders.

Run hourly (cron / Windows Task Scheduler):
    python manage.py process_payment_reminders

For each open order that must be paid online before delivery (payment_due_at is set):
  * sends reminder N when settings.PAYMENT_REMINDER_HOURS[N] hours have passed since the
    deadline clock started, and
  * once payment_due_at has passed, switches the order to cash on delivery so the laundry
    can still be delivered, and tells the customer and the shop.

Idempotent: reminders are counted on the order, and switched orders drop out of the query.
"""
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from notifications.models import Notification
from orders.models import Order
from payments.models import PayMongoTransaction
from utils.email_notifications import send_email_notification


class Command(BaseCommand):
    help = 'Send payment reminders for unpaid online orders and switch overdue ones to cash on delivery.'

    def add_arguments(self, parser):
        parser.add_argument('--dry-run', action='store_true', help='Report what would happen without changing anything.')

    def handle(self, *args, dry_run=False, **options):
        now = timezone.now()
        reminder_hours = sorted(settings.PAYMENT_REMINDER_HOURS)
        due_window = timedelta(hours=settings.PAYMENT_DUE_HOURS)
        reminded = switched = 0

        orders = (
            Order.objects.filter(payment_due_at__isnull=False, payment_status__in=['unpaid', 'failed'])
            .exclude(payment_method='cod')
            .exclude(status__in=['out_for_delivery', 'delivered', 'cancelled', 'declined'])
            .select_related('customer', 'shop__owner')
            .order_by('payment_due_at')     # walks the small partial index (order_due_idx), soonest first
        )
        for order in orders:
            if PayMongoTransaction.in_progress(order).exists():
                continue                                  # customer is mid-payment: leave it alone this round

            if now >= order.payment_due_at:
                switched += 1
                if not dry_run:
                    self._switch_to_cod(order)
                self.stdout.write(f'Order #{order.id}: deadline passed -> switched to cash on delivery')
                continue

            clock_started = order.payment_due_at - due_window
            due_reminders = sum(1 for h in reminder_hours if now >= clock_started + timedelta(hours=h))
            if due_reminders > order.payment_reminders_sent:
                reminded += 1
                if not dry_run:
                    self._remind(order, due_reminders)
                self.stdout.write(f'Order #{order.id}: reminder {due_reminders} of {len(reminder_hours)}')

        prefix = '[dry run] ' if dry_run else ''
        self.stdout.write(self.style.SUCCESS(f'{prefix}{reminded} reminder(s) sent, {switched} order(s) switched to COD.'))

    def _remind(self, order, reminder_number):
        due = timezone.localtime(order.payment_due_at)
        with transaction.atomic():
            # Re-check under lock so two overlapping runs can't double-send.
            locked = Order.objects.select_for_update().get(pk=order.pk)
            if locked.payment_reminders_sent >= reminder_number or locked.payment_status == 'paid':
                return
            locked.payment_reminders_sent = reminder_number
            locked.save(update_fields=['payment_reminders_sent'])
            Notification.objects.create(
                user=order.customer,
                message=(f'Reminder: please pay ₱{order.total_amount:,.2f} for order #{order.id} by '
                         f'{due:%b %d, %I:%M %p}. After that it switches to cash on delivery.'),
                link=f'/orders/{order.id}/',
            )
        send_email_notification('payment_reminder', order.customer, order=order,
                                extra={'due': f'{due:%b %d, %I:%M %p}'})

    def _switch_to_cod(self, order):
        with transaction.atomic():
            locked = Order.objects.select_for_update().get(pk=order.pk)
            if locked.payment_status == 'paid' or locked.payment_method == 'cod':
                return
            locked.payment_method = 'cod'
            locked.payment_status = 'unpaid'
            locked.payment_due_at = None
            locked.save(update_fields=['payment_method', 'payment_status', 'payment_due_at', 'updated_at'])
            Notification.objects.create(
                user=order.customer,
                message=(f'Order #{order.id} was not paid online in time, so it is now cash on delivery. '
                         f'Please pay the rider ₱{order.total_amount:,.2f} when your laundry arrives.'),
                link=f'/orders/{order.id}/',
            )
            Notification.objects.create(
                user=order.shop.owner,
                message=f'Order #{order.id} was not paid online by the deadline and is now cash on delivery.',
                link=f'/shop-dashboard/orders/{order.id}/',
            )
        send_email_notification('payment_switched_cod', order.customer, order=order)
