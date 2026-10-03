import uuid
from pathlib import Path

from django.core.validators import MinValueValidator
from django.db import models
from django.conf import settings
from django.utils import timezone
from utils.validators import validate_document_upload, validate_image_upload


def _shop_logo_upload(instance, filename):
    return f'shop_logos/{uuid.uuid4().hex}{Path(filename).suffix.lower()}'


def _shop_banner_upload(instance, filename):
    return f'shop_banners/{uuid.uuid4().hex}{Path(filename).suffix.lower()}'


def _shop_owner_doc_upload(instance, filename):
    # Private: served only through shops.views.serve_shop_doc_view (owner/admin).
    return f'shop_docs/{uuid.uuid4().hex}{Path(filename).suffix.lower()}'


class Shop(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
        ('suspended', 'Suspended'),
    ]

    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='shops')
    name = models.CharField(max_length=200)
    tagline = models.CharField(max_length=200, blank=True)
    description = models.TextField(blank=True)
    address = models.TextField()
    contact = models.CharField(max_length=50, blank=True)
    logo = models.ImageField(upload_to=_shop_logo_upload, blank=True, null=True, validators=[validate_image_upload])
    banner = models.ImageField(upload_to=_shop_banner_upload, blank=True, null=True, validators=[validate_image_upload])
    owner_government_id = models.FileField(upload_to=_shop_owner_doc_upload, blank=True, null=True, validators=[validate_document_upload])
    owner_selfie_with_id = models.ImageField(upload_to=_shop_owner_doc_upload, blank=True, null=True, validators=[validate_image_upload])
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    max_orders_per_day = models.PositiveIntegerField(default=20)
    operating_hours_start = models.TimeField(default='08:00')
    operating_hours_end = models.TimeField(default='18:00')
    minimum_load_kg = models.DecimalField(max_digits=6, decimal_places=2, null=True, blank=True)
    turnaround = models.CharField(max_length=50, blank=True)
    payments = models.JSONField(default=list, blank=True)
    unclaimed_policy = models.CharField(max_length=200, blank=True)
    customer_notes = models.TextField(blank=True)
    rejection_reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name

    @property
    def is_open(self):
        now = timezone.localtime().time()
        return self.operating_hours_start <= now <= self.operating_hours_end

    def average_rating(self):
        # Memoized per instance: shop pages show the rating several times (incl. a 5-star loop).
        if not hasattr(self, '_average_rating'):
            from django.db.models import Avg
            result = self.reviews.aggregate(avg=Avg('rating'))['avg']
            self._average_rating = round(result, 1) if result is not None else 0
        return self._average_rating

    def reviews_total(self):
        if not hasattr(self, '_reviews_total'):
            self._reviews_total = self.reviews.count()
        return self._reviews_total

    def save(self, *args, **kwargs):
        from utils.images import shrink_uploaded_image
        shrink_uploaded_image(self.logo, max_px=800)
        shrink_uploaded_image(self.banner, max_px=1920)
        shrink_uploaded_image(self.owner_selfie_with_id, max_px=2000)   # ID must stay readable for review
        if self.address and (not self.latitude or not self.longitude):
            from utils.geocode import geocode_address
            lat, lng = geocode_address(self.address)
            if lat:
                self.latitude = lat
                self.longitude = lng
        super().save(*args, **kwargs)


class Service(models.Model):
    SERVICE_CHOICES = [
        ('wash', 'Wash'),
        ('dry', 'Dry'),
        ('fold', 'Fold'),
        ('iron', 'Iron'),
        ('dry_clean', 'Dry Clean'),
    ]

    shop = models.ForeignKey(Shop, on_delete=models.CASCADE, related_name='services')
    name = models.CharField(max_length=50, choices=SERVICE_CHOICES)
    price_per_kg = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True, validators=[MinValueValidator(0)])
    price_per_piece = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True, validators=[MinValueValidator(0)])

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['shop', 'name'], name='unique_service_per_shop'),
        ]

    def __str__(self):
        return f'{self.shop.name} - {self.get_name_display()}'


class DeliverySlot(models.Model):
    shop = models.ForeignKey(Shop, on_delete=models.CASCADE, related_name='delivery_slots')
    date = models.DateField()
    time_start = models.TimeField()
    time_end = models.TimeField()
    max_capacity = models.PositiveIntegerField(default=5)
    current_bookings = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['date', 'time_start']
        constraints = [
            models.UniqueConstraint(fields=['shop', 'date', 'time_start', 'time_end'], name='unique_delivery_slot_per_shop'),
            models.CheckConstraint(check=models.Q(max_capacity__gte=1), name='delivery_slot_positive_capacity'),
            models.CheckConstraint(check=models.Q(current_bookings__gte=0), name='delivery_slot_non_negative_bookings'),
            models.CheckConstraint(check=models.Q(time_end__gt=models.F('time_start')), name='delivery_slot_valid_time_range'),
        ]

    def __str__(self):
        return f'{self.shop.name} - {self.date} {self.time_start}-{self.time_end}'

    @property
    def is_available(self):
        return self.current_bookings < self.max_capacity

    @property
    def available_capacity(self):
        return max(self.max_capacity - self.current_bookings, 0)

    @property
    def capacity_percent(self):
        if self.max_capacity == 0:
            return 100
        return int((self.current_bookings / self.max_capacity) * 100)


class PromotionQuerySet(models.QuerySet):
    def live(self):
        """Active promos whose date window includes today (Asia/Manila)."""
        today = timezone.localdate()
        return self.filter(is_active=True, start_date__lte=today, end_date__gte=today)


class Promotion(models.Model):
    DISCOUNT_TYPES = [
        ('percent', 'Percent off'),
        ('fixed', 'Fixed amount off (₱)'),
        ('offer', 'Special offer (no fixed discount)'),
    ]

    shop = models.ForeignKey(Shop, on_delete=models.CASCADE, related_name='promotions')
    title = models.CharField(max_length=100)
    description = models.CharField(max_length=500, blank=True)
    discount_type = models.CharField(max_length=10, choices=DISCOUNT_TYPES, default='percent')
    discount_value = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True,
                                         validators=[MinValueValidator(0)])
    min_order_amount = models.DecimalField(max_digits=8, decimal_places=2, null=True, blank=True,
                                           validators=[MinValueValidator(0)])
    start_date = models.DateField()
    end_date = models.DateField()
    is_active = models.BooleanField(default=True, help_text='Uncheck to pause without deleting.')
    created_at = models.DateTimeField(auto_now_add=True)

    objects = PromotionQuerySet.as_manager()

    class Meta:
        ordering = ['start_date', 'end_date', 'id']
        constraints = [
            models.CheckConstraint(check=models.Q(end_date__gte=models.F('start_date')), name='promotion_valid_date_range'),
        ]

    def __str__(self):
        return f'{self.shop.name} — {self.title}'

    def clean(self):
        from django.core.exceptions import ValidationError
        errors = {}
        if self.start_date and self.end_date and self.end_date < self.start_date:
            errors['end_date'] = 'End date cannot be before the start date.'
        if self.discount_type == 'percent':
            # Capped at 90% so an automatically applied promo never zeroes an order.
            if self.discount_value is None or not (0 < self.discount_value <= 90):
                errors['discount_value'] = 'Percent discount must be between 1 and 90.'
        elif self.discount_type == 'fixed':
            if self.discount_value is None or not (0 < self.discount_value <= 50000):
                errors['discount_value'] = 'Fixed discount must be between ₱1 and ₱50,000.'
        else:
            self.discount_value = None
        if errors:
            raise ValidationError(errors)

    @property
    def discount_label(self):
        if self.discount_type == 'percent' and self.discount_value is not None:
            return f'{self.discount_value.normalize():f}% off'
        if self.discount_type == 'fixed' and self.discount_value is not None:
            return f'₱{self.discount_value:,.2f} off'
        return 'Special offer'

    def discount_for(self, subtotal):
        """Peso discount this promo gives on `subtotal`, or None if it doesn't apply.

        Special offers never deduct; the minimum order must be met; and the
        discount must leave something to pay (it can't equal or exceed the subtotal).
        """
        from decimal import Decimal, ROUND_HALF_UP
        if self.discount_type not in ('percent', 'fixed') or self.discount_value is None:
            return None
        if self.min_order_amount is not None and subtotal < self.min_order_amount:
            return None
        if self.discount_type == 'percent':
            amount = (subtotal * self.discount_value / Decimal('100')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        else:
            amount = self.discount_value
        if amount <= 0 or amount >= subtotal:
            return None
        return amount

    @classmethod
    def best_for(cls, shop, subtotal):
        """(promo, discount) for the live promo saving the most on this subtotal, else (None, 0)."""
        from decimal import Decimal
        best, best_amount = None, Decimal('0.00')
        for promo in shop.promotions.live().order_by('id'):
            amount = promo.discount_for(subtotal)
            if amount is not None and amount > best_amount:
                best, best_amount = promo, amount
        return best, best_amount

    @property
    def status(self):
        """live | scheduled | expired | paused — for the owner's list."""
        today = timezone.localdate()
        if not self.is_active:
            return 'paused'
        if today < self.start_date:
            return 'scheduled'
        if today > self.end_date:
            return 'expired'
        return 'live'
