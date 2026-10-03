from django.db import models
from django.conf import settings
from orders.models import Order


class PayMongoTransaction(models.Model):
    STATUS_CHOICES = [
        ('awaiting_payment_method', 'Awaiting Payment Method'),
        ('processing', 'Processing'),
        ('succeeded', 'Succeeded'),
        ('failed', 'Failed'),
        ('refunded', 'Refunded'),
    ]

    order = models.OneToOneField(Order, on_delete=models.CASCADE, related_name='paymongo_transaction')
    payment_intent_id = models.CharField(max_length=200, blank=True, db_index=True)   # webhook lookups
    payment_method_id = models.CharField(max_length=200, blank=True)
    # e-wallet chosen at checkout (gcash/grab_pay/maya); applied to the order when payment succeeds,
    # so a cash-on-delivery order that is paid online afterwards records how it was actually paid.
    payment_method_type = models.CharField(max_length=20, blank=True)
    payment_id = models.CharField(max_length=200, blank=True)
    amount = models.BigIntegerField(help_text='Amount in centavos')
    currency = models.CharField(max_length=3, default='PHP')
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default='awaiting_payment_method')
    paymongo_event_type = models.CharField(max_length=100, blank=True)
    raw_payload = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    IN_PROGRESS = ('awaiting_payment_method', 'processing')
    # An abandoned e-wallet redirect never reports back; after this long it no longer blocks
    # delivery, switching to cash on delivery, or the payment deadline.
    STALE_AFTER_MINUTES = 30

    @classmethod
    def in_progress(cls, order):
        from datetime import timedelta
        from django.utils import timezone
        cutoff = timezone.now() - timedelta(minutes=cls.STALE_AFTER_MINUTES)
        return cls.objects.filter(order=order, status__in=cls.IN_PROGRESS, updated_at__gte=cutoff)

    def __str__(self):
        return f'Transaction for Order #{self.order_id}'


class Refund(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('succeeded', 'Succeeded'),
        ('failed', 'Failed'),
    ]

    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='refunds')
    paymongo_refund_id = models.CharField(max_length=200, blank=True, db_index=True)   # refund webhooks
    amount = models.BigIntegerField(help_text='Amount in centavos')
    reason = models.CharField(max_length=200)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    initiated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'Refund for Order #{self.order_id}'


class WebhookEvent(models.Model):
    event_id = models.CharField(max_length=200, unique=True)
    event_type = models.CharField(max_length=100, blank=True)
    processed_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'{self.event_type} — {self.event_id}'
