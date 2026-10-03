import uuid
from pathlib import Path

from django.db import models
from django.conf import settings
from shops.models import Shop, Service, DeliverySlot
from utils.validators import validate_image_upload


def _proof_photo_upload(instance, filename):
    return f'proof_photos/{uuid.uuid4().hex}{Path(filename).suffix.lower()}'


class Order(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('accepted', 'Accepted'),
        ('declined', 'Declined'),
        ('picked_up', 'Picked Up'),
        ('washing', 'Washing'),
        ('drying', 'Drying'),
        ('folding', 'Folding'),
        ('ready', 'Ready for Delivery'),
        ('out_for_delivery', 'Out for Delivery'),
        ('delivered', 'Delivered'),
        ('cancelled', 'Cancelled'),
    ]
    PAYMENT_METHOD_CHOICES = [
        ('gcash', 'GCash'),
        ('grab_pay', 'GrabPay'),
        ('maya', 'Maya'),
        ('card', 'Credit/Debit Card'),
        ('online_banking', 'Online Banking'),
        ('cod', 'Cash on Delivery'),
    ]
    PAYMENT_STATUS_CHOICES = [
        ('unpaid', 'Unpaid'),
        ('paid', 'Paid'),
        ('failed', 'Failed'),
        ('refunded', 'Refunded'),
    ]

    customer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='orders')
    shop = models.ForeignKey(Shop, on_delete=models.CASCADE, related_name='orders')
    rider = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='rider_orders')
    services = models.ManyToManyField(Service, through='OrderService')
    pickup_address = models.TextField()
    delivery_address = models.TextField()
    pickup_lat = models.FloatField(null=True, blank=True)
    pickup_lng = models.FloatField(null=True, blank=True)
    delivery_lat = models.FloatField(null=True, blank=True)
    delivery_lng = models.FloatField(null=True, blank=True)
    pickup_datetime = models.DateTimeField()
    delivery_slot = models.ForeignKey(DeliverySlot, on_delete=models.SET_NULL, null=True, blank=True)
    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default='pending')
    payment_method = models.CharField(max_length=20, choices=PAYMENT_METHOD_CHOICES)
    payment_status = models.CharField(max_length=20, choices=PAYMENT_STATUS_CHOICES, default='unpaid')
    paymongo_payment_intent_id = models.CharField(max_length=200, blank=True)
    paymongo_payment_method_id = models.CharField(max_length=200, blank=True)
    total_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)  # amount charged (after discount)
    subtotal_amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)  # before discount
    discount_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    promotion = models.ForeignKey('shops.Promotion', on_delete=models.SET_NULL, null=True, blank=True, related_name='orders')
    promotion_title = models.CharField(max_length=100, blank=True)  # snapshot: survives promo edits/deletion
    # "Let the shop weigh it": per-kg lines have no weight until the shop weighs the laundry
    # at drop-off; payment (and promo eligibility) wait for that confirmed price.
    weigh_at_shop = models.BooleanField(default=False)
    price_confirmed_at = models.DateTimeField(null=True, blank=True)
    price_confirmed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                           related_name='price_confirmed_orders')
    # Online payment deadline: reminders go out before it; after it the order switches to COD
    # (see orders/management/commands/process_payment_reminders.py).
    payment_due_at = models.DateTimeField(null=True, blank=True)
    payment_reminders_sent = models.PositiveSmallIntegerField(default=0)
    notes = models.TextField(blank=True)
    decline_reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            # Newest-first lists (admin orders, "latest" widgets) and analytics date ranges.
            models.Index(fields=['-created_at'], name='order_created_idx'),
            # Shop order list: WHERE shop_id = ? ORDER BY created_at DESC LIMIT 25 (no sort step).
            models.Index(fields=['shop', '-created_at'], name='order_shop_created_idx'),
            # Status filters: rider job board (status='accepted'), landing delivered count, admin tabs.
            models.Index(fields=['status'], name='order_status_idx'),
            # PayMongo webhook / return look orders up by intent id.
            models.Index(fields=['paymongo_payment_intent_id'], name='order_pi_idx'),
            # Hourly payment-reminder job; only the few orders with a deadline are indexed.
            models.Index(fields=['payment_due_at'], name='order_due_idx',
                         condition=models.Q(payment_due_at__isnull=False)),
        ]

    def __str__(self):
        return f'Order #{self.id} - {self.customer.email}'

    @property
    def price_pending(self):
        """True while a weigh-at-shop order is waiting for the shop to enter the real weight."""
        return self.weigh_at_shop and self.price_confirmed_at is None

    @property
    def awaiting_online_payment(self):
        return self.payment_method != 'cod' and self.payment_status != 'paid'

    @property
    def can_pay_online(self):
        """Online payment is open once the price is final, until delivery.

        Cash-on-delivery orders can switch to paying online too (e.g. after the shop weighs the
        laundry), but not once the rider is out delivering and collecting cash.
        """
        if self.payment_status == 'paid' or self.price_pending:
            return False
        if self.status in ('cancelled', 'declined', 'delivered'):
            return False
        if self.payment_method == 'cod' and self.status == 'out_for_delivery':
            return False
        return True

    def online_payment_in_progress(self):
        from payments.models import PayMongoTransaction   # avoid a circular import at module load
        return PayMongoTransaction.in_progress(self).exists()

    def start_payment_clock(self, now=None):
        """(Re)start the online-payment deadline. Call when the price becomes final; caller saves."""
        from datetime import timedelta
        from django.utils import timezone
        if self.payment_method == 'cod' or self.payment_status == 'paid':
            self.payment_due_at = None
        else:
            self.payment_due_at = (now or timezone.now()) + timedelta(hours=settings.PAYMENT_DUE_HOURS)
        self.payment_reminders_sent = 0

    def delivery_block_reason(self):
        """Why the order can't go out for delivery yet (None if it can): price first, then payment."""
        if self.price_pending:
            return "The shop hasn't confirmed the weight and final price yet."
        if self.payment_status != 'paid' and self.online_payment_in_progress():
            return 'The customer is completing an online payment right now. Try again in a few minutes.'
        if self.awaiting_online_payment:
            return "The customer hasn't paid online yet. Delivery starts after payment (or after they switch to cash on delivery)."
        return None


class OrderService(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='order_services')
    # RESTRICT keeps order history intact when an owner removes a service; a
    # full shop/user delete still cascades through the Order relation.
    service = models.ForeignKey(Service, on_delete=models.RESTRICT)
    quantity = models.PositiveIntegerField(default=1)
    weight_kg = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    subtotal = models.DecimalField(max_digits=8, decimal_places=2, default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['order', 'service'], name='unique_order_service'),
            models.CheckConstraint(check=models.Q(quantity__gte=0), name='order_service_non_negative_quantity'),
            models.CheckConstraint(
                check=models.Q(weight_kg__isnull=True) | models.Q(weight_kg__gte=0),
                name='order_service_non_negative_weight',
            ),
            models.CheckConstraint(check=models.Q(subtotal__gte=0), name='order_service_non_negative_subtotal'),
        ]

    def __str__(self):
        return f'{self.order} - {self.service.get_name_display()}'


class OrderStatusLog(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='status_logs')
    status = models.CharField(max_length=30, choices=Order.STATUS_CHOICES)
    note = models.TextField(blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['timestamp']


class PhotoProof(models.Model):
    PROOF_TYPES = [
        ('pickup', 'Pickup'),
        ('dropoff', 'Shop Drop-Off'),
        ('delivery', 'Delivery'),
        ('weight', 'Scale Reading'),
    ]
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='photo_proofs')
    photo = models.ImageField(upload_to=_proof_photo_upload, validators=[validate_image_upload])
    proof_type = models.CharField(max_length=20, choices=PROOF_TYPES)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)

    def save(self, *args, **kwargs):
        from utils.images import shrink_uploaded_image
        shrink_uploaded_image(self.photo, max_px=1920)     # proof photos stay legible at full screen
        super().save(*args, **kwargs)


class EmailLog(models.Model):
    recipient_email = models.EmailField()
    subject = models.CharField(max_length=300)
    body_preview = models.TextField()
    formspree_response_status = models.IntegerField(null=True, blank=True)
    sent_at = models.DateTimeField(auto_now_add=True)
    order = models.ForeignKey(Order, on_delete=models.SET_NULL, null=True, blank=True, related_name='email_logs')
    triggered_by = models.CharField(max_length=50, blank=True)
